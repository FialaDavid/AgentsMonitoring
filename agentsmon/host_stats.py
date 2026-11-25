"""Best-effort macOS host metrics for the dashboard."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time

_latest = None
_previous_network = None


def _run(*args):
    return subprocess.run(args, capture_output=True, text=True, timeout=5, check=True).stdout


def _cpu():
    try:
        output = _run("top", "-l", "2", "-n", "0", "-s", "1")
        rows = re.findall(r"CPU usage:\s*([\d.]+)% user,\s*([\d.]+)% sys", output)
        if len(rows) < 2:
            return None
        user, system = map(float, rows[1])
        try:
            load = list(os.getloadavg())
        except (OSError, AttributeError):
            load = None
        return {"percent": user + system, "load": load, "cores": os.cpu_count()}
    except Exception:
        return None


def _memory():
    try:
        total = int(_run("sysctl", "-n", "hw.memsize").strip())
        vm = _run("vm_stat")
        page = re.search(r"page size of (\d+) bytes", vm)
        if not page:
            return None
        values = {name: int(float(count.replace(",", ""))) for name, count in
                  re.findall(r"^(Pages (?:active|wired down|occupied by compressor)):\s*([\d.]+)", vm, re.M)}
        active = values["Pages active"]
        wired = values["Pages wired down"]
        compressed = values["Pages occupied by compressor"]
        size = int(page.group(1))
        used = (active + wired + compressed) * size
        pressure = re.search(r"System-wide memory free percentage:\s*(\d+)%", _run("memory_pressure"))
        swap = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", _run("sysctl", "-n", "vm.swapusage"))
        if not pressure or not swap:
            return None
        return {"total": total, "used": used, "percent": used / total * 100,
                "wired": wired * size, "compressed": compressed * size,
                "pressure_free_percent": int(pressure.group(1)),
                "swap_used": float(swap.group(2)) * 1024 * 1024,
                "swap_total": float(swap.group(1)) * 1024 * 1024}
    except Exception:
        return None


def _network():
    global _previous_network
    try:
        route = _run("route", "-n", "get", "default")
        match = re.search(r"^\s*interface:\s*(\S+)", route, re.M)
        if not match:
            return None
        interface = match.group(1)
        output = _run("netstat", "-ibn")
        counters = None
        for line in output.splitlines():
            fields = line.split()
            if len(fields) >= 10 and fields[0] == interface and fields[2].startswith("<Link#"):
                counters = (int(fields[6]), int(fields[9]))
                break
        if counters is None:
            return None
        now = time.monotonic()
        previous = _previous_network
        _previous_network = (now, counters)
        rx_bps = tx_bps = None
        if previous and now > previous[0]:
            elapsed = now - previous[0]
            rx_bps = max(0, counters[0] - previous[1][0]) / elapsed
            tx_bps = max(0, counters[1] - previous[1][1]) / elapsed
        return {"interface": interface, "rx_bytes": counters[0], "tx_bytes": counters[1],
                "rx_bps": rx_bps, "tx_bps": tx_bps}
    except Exception:
        return None


def _disk(path="/"):
    try:
        usage = shutil.disk_usage(path)
        return {"total": usage.total, "used": usage.used, "free": usage.free,
                "percent": usage.used / usage.total * 100}
    except Exception:
        return None


def _uptime():
    try:
        output = _run("sysctl", "-n", "kern.boottime")
        match = re.search(r"sec = (\d+)", output)
        return time.time() - int(match.group(1)) if match else None
    except Exception:
        return None


def sample(disk_path="/"):
    if sys.platform != "darwin":
        return None
    return {"cpu": _cpu(), "memory": _memory(), "network": _network(),
            "disk": _disk(disk_path), "uptime": _uptime()}


def tick(disk_path="/"):
    global _latest
    data = sample(disk_path)
    _latest = {"timestamp": time.time(), **data} if data else None
    return _latest


def latest():
    return _latest
