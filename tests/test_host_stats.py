import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentsmon import config, host_stats


TOP = """CPU usage: 1.0% user, 2.0% sys, 97.0% idle\nCPU usage: 10.0% user, 5.0% sys, 85.0% idle\n"""
VM = """Mach Virtual Memory Statistics: (page size of 4096 bytes)\nPages free: 100.\nPages active: 200.\nPages wired down: 50.\nPages occupied by compressor: 25.\n"""
PRESSURE = "System-wide memory free percentage: 32%\n"


def result(stdout):
    return SimpleNamespace(stdout=stdout, returncode=0)


def command_output(args, rx=1000, tx=2000):
    if args[:3] == ("top", "-l", "2"):
        return TOP
    if args == ("sysctl", "-n", "hw.memsize"):
        return "4194304\n"
    if args == ("vm_stat",):
        return VM
    if args == ("memory_pressure",):
        return PRESSURE
    if args == ("sysctl", "-n", "vm.swapusage"):
        return "total = 100.00M used = 25.00M free = 75.00M\n"
    if args == ("route", "-n", "get", "default"):
        return "gateway: 192.0.2.1\n  interface: en0\n"
    if args == ("netstat", "-ibn"):
        return f"Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll\nen0 1500 <Link#4> aa 1 0 {rx} 2 0 {tx} 0\n"
    if args == ("sysctl", "-n", "kern.boottime"):
        return "{ sec = 1700000000, usec = 0 }\n"
    raise AssertionError(args)


class HostStats(unittest.TestCase):
    def setUp(self):
        host_stats._previous_network = None
        host_stats._latest = None

    def test_mac_sample_parses_captured_outputs(self):
        with patch.object(host_stats.sys, "platform", "darwin"), \
             patch.object(host_stats.subprocess, "run", side_effect=lambda *a, **k: result(command_output(a[0]))), \
             patch.object(host_stats.shutil, "disk_usage", return_value=SimpleNamespace(total=1000, used=400, free=600)), \
             patch.object(host_stats.os, "getloadavg", return_value=(1.0, 2.0, 3.0)), \
             patch.object(host_stats.os, "cpu_count", return_value=10), \
             patch.object(host_stats.time, "time", return_value=1700000100):
            data = host_stats.sample()
        self.assertEqual(data["cpu"], {"percent": 15.0, "load": [1.0, 2.0, 3.0], "cores": 10})
        self.assertEqual(data["memory"], {"total": 4194304, "used": 1126400, "percent": 26.85546875,
                         "wired": 204800, "compressed": 102400, "pressure_free_percent": 32,
                         "swap_used": 25 * 1048576, "swap_total": 100 * 1048576})
        self.assertEqual(data["network"]["interface"], "en0")
        self.assertIsNone(data["network"]["rx_bps"])
        self.assertEqual(data["disk"], {"total": 1000, "used": 400, "free": 600, "percent": 40.0})
        self.assertEqual(data["uptime"], 100)

    def test_non_default_disk_path_reaches_disk_usage(self):
        with patch.object(host_stats.sys, "platform", "darwin"), \
             patch.object(host_stats, "_cpu", return_value=None), \
             patch.object(host_stats, "_memory", return_value=None), \
             patch.object(host_stats, "_network", return_value=None), \
             patch.object(host_stats, "_uptime", return_value=None), \
             patch.object(host_stats.shutil, "disk_usage", return_value=SimpleNamespace(total=1000, used=400, free=600)) as usage:
            host_stats.sample("/Volumes/Data")
        usage.assert_called_once_with("/Volumes/Data")

    def test_network_rates_are_delta_over_elapsed_time(self):
        outputs = iter([(1000, 2000), (5000, 6000)])
        def run(*args, **kwargs):
            return result(command_output(args[0], *next(outputs)) if args[0] == ("netstat", "-ibn")
                          else command_output(args[0]))
        with patch.object(host_stats.subprocess, "run", side_effect=run), \
             patch.object(host_stats.time, "monotonic", side_effect=[10.0, 12.0]):
            first, second = host_stats._network(), host_stats._network()
        self.assertIsNone(first["rx_bps"])
        self.assertEqual(second["rx_bps"], 2000)
        self.assertEqual(second["tx_bps"], 2000)

    def test_failed_cpu_command_only_blanks_cpu(self):
        def run(*args, **kwargs):
            if args[0] == ("top", "-l", "2", "-n", "0", "-s", "1"):
                raise subprocess.CalledProcessError(1, args[0])
            return result(command_output(args[0]))
        with patch.object(host_stats.sys, "platform", "darwin"), \
             patch.object(host_stats.subprocess, "run", side_effect=run), \
             patch.object(host_stats.shutil, "disk_usage", return_value=SimpleNamespace(total=1000, used=400, free=600)):
            data = host_stats.sample()
        self.assertIsNone(data["cpu"])
        self.assertIsNotNone(data["memory"])
        self.assertIsNotNone(data["network"])
        self.assertIsNotNone(data["disk"])
        self.assertIsNotNone(data["uptime"])

    def test_non_macos_returns_none(self):
        with patch.object(host_stats.sys, "platform", "linux"):
            self.assertIsNone(host_stats.sample())

    def test_host_stats_are_disabled_by_default(self):
        self.assertEqual(config.DEFAULTS["host_stats"], {"enabled": False, "interval_seconds": 5,
                         "network_max_mbps": 1000, "warn_percent": 70, "critical_percent": 90,
                         "disk_path": "/"})

    def test_state_host_is_none_when_disabled(self):
        from agentsmon import dashboard
        cfg = {"host_stats": {"enabled": False}}
        with patch.object(config, "load", return_value=cfg), \
             patch.object(dashboard, "_agents_state", return_value=[]), \
             patch.object(dashboard.plugins, "collect", return_value={"hidden_sessions": [], "patches": [],
                 "tables": [], "columns": [], "errors": [], "plugins": [], "loading": False}), \
             patch.object(dashboard, "_service_state", return_value=[]), \
             patch("agentsmon.integrity.status", return_value={"version": "test", "state": "clean"}):
            self.assertIsNone(json.loads(dashboard._state())["host"])

    def test_state_exposes_host_dial_settings_from_merged_config(self):
        from agentsmon import dashboard
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg_path = Path(temp_dir) / "config.json"
            cfg_path.write_text(json.dumps({"host_stats": {"enabled": True}}), encoding="utf-8")
            with patch.object(config, "DEFAULT_PATH", cfg_path), \
                 patch.object(config, "discover_modules", return_value=[]), \
                 patch.object(dashboard, "_agents_state", return_value=[]), \
                 patch.object(dashboard.plugins, "collect", return_value={"hidden_sessions": [], "patches": [],
                     "tables": [], "columns": [], "errors": [], "plugins": [], "loading": False}), \
                 patch.object(dashboard, "_service_state", return_value=[]), \
                 patch.object(dashboard, "_scheduled_jobs_state", return_value=None), \
                 patch.object(host_stats, "latest", return_value={"cpu": None}), \
                 patch("agentsmon.integrity.status", return_value={"version": "test", "state": "clean"}):
                self.assertEqual(json.loads(dashboard._state())["host"]["settings"],
                    {"network_max_mbps": 1000, "warn_percent": 70, "critical_percent": 90})
                cfg_path.write_text(json.dumps({"host_stats": {"enabled": True, "network_max_mbps": 2500,
                    "warn_percent": 65, "critical_percent": 88}}), encoding="utf-8")
                self.assertEqual(json.loads(dashboard._state())["host"]["settings"],
                    {"network_max_mbps": 2500, "warn_percent": 65, "critical_percent": 88})

            with patch.object(config, "DEFAULT_PATH", cfg_path), \
                 patch.object(config, "discover_modules", return_value=[]):
                cfg_path.write_text("{}", encoding="utf-8")
                merged = config.load()
            self.assertEqual({k: merged["host_stats"][k] for k in
                ("network_max_mbps", "warn_percent", "critical_percent")},
                {"network_max_mbps": 1000, "warn_percent": 70, "critical_percent": 90})


if __name__ == "__main__":
    unittest.main()
