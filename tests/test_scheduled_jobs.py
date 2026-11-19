import datetime as dt
import json
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agentsmon import config, scheduled_jobs


class ScheduledJobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.launchd = self.root / "LaunchAgents"
        self.launchd.mkdir()
        self.hermes = self.root / "hermes"
        self.hermes.mkdir()
        self.patch = patch.object(config, "state_dir", lambda: self.state)
        self.patch.start()
        self.now = dt.datetime(2026, 11, 19, 10, 0, tzinfo=dt.timezone.utc)
        self.cfg = {"scheduled_jobs": {"enabled": True, "interval_seconds": 60, "grace_seconds": 900,
                                      "sources": ["launchd", "hermes"], "ignore": [],
                                      "launchd_dir": str(self.launchd), "hermes_cron_dir": str(self.hermes),
                                      "notify_command": []}}

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def plist(self, label, **extra):
        data = {"Label": label, **extra}
        path = self.launchd / (label + ".plist")
        path.write_bytes(plistlib.dumps(data))
        return path

    def runner(self, runs=0, exit_code=0):
        def run(argv):
            from types import SimpleNamespace
            return SimpleNamespace(returncode=0, stdout="runs = %d\nlast exit code = %d" % (runs, exit_code))
        return run

    def tick(self, when, runs=0, exit_code=0):
        self.cfg["launchctl_runner"] = self.runner(runs, exit_code)
        return scheduled_jobs.tick(self.cfg, when)

    def test_calendar_fired_is_ok_with_approximate_last_run(self):
        self.plist("calendar", StartCalendarInterval={"Hour": 10, "Minute": 0})
        self.tick(self.now - dt.timedelta(minutes=1), 1)
        result = self.tick(self.now, 2)
        job = result["jobs"][0]
        self.assertEqual(job["state"], "ok")
        self.assertIsNotNone(job["last_run_at"])

    def test_calendar_match_uses_latest_time_when_fields_are_omitted(self):
        now = self.now.replace(minute=30)
        target = scheduled_jobs._recent_calendar([{"Hour": 10}], now)
        self.assertEqual(dt.datetime.fromtimestamp(target, now.tzinfo).hour, 10)
        self.assertEqual(dt.datetime.fromtimestamp(target, now.tzinfo).minute, 30)

    def test_calendar_missed_when_sampling_covers_expected_fire(self):
        self.plist("calendar", StartCalendarInterval={"Hour": 10, "Minute": 0})
        for minute in range(-1, 17):
            self.tick(self.now + dt.timedelta(minutes=minute), 0)
        self.assertEqual(json.loads((self.state / "scheduled_jobs.json").read_text())["jobs"][0]["state"], "missed")

    def test_sleep_gap_is_not_missed(self):
        self.plist("calendar", StartCalendarInterval={"Hour": 10, "Minute": 0})
        self.tick(self.now - dt.timedelta(minutes=1), 0)
        result = self.tick(self.now + dt.timedelta(minutes=20), 0)
        self.assertNotEqual(result["jobs"][0]["state"], "missed")

    def test_reset_becomes_new_baseline_without_old_miss(self):
        self.plist("calendar", StartCalendarInterval={"Hour": 10, "Minute": 0})
        # Establish pre-fire baseline, then a reset before the expected time.
        self.tick(self.now - dt.timedelta(minutes=1), 5)
        self.tick(self.now, 0)
        result = self.tick(self.now + dt.timedelta(minutes=16), 0)
        self.assertNotEqual(result["jobs"][0]["state"], "missed")

    def test_nonzero_exit_and_keepalive(self):
        self.plist("failed", StartInterval=60)
        self.plist("daemon", StartInterval=60, KeepAlive=True)
        result = self.tick(self.now, 0, 3)
        self.assertEqual([(j["id"], j["state"]) for j in result["jobs"]], [("failed", "failed")])

    def test_ignore_pattern_excludes_launchd_job(self):
        self.plist("ignored-job", StartInterval=60)
        self.cfg["scheduled_jobs"]["ignore"] = ["ignored-*"]
        self.assertEqual(self.tick(self.now)["jobs"], [])

    def hermes_files(self, jobs, executions=()):
        (self.hermes / "jobs.json").write_text(json.dumps({"jobs": jobs}))
        con = sqlite3.connect(self.hermes / "executions.db")
        con.execute("CREATE TABLE executions (job_id TEXT, status TEXT, scheduled_instant TEXT, finished_at TEXT)")
        con.executemany("INSERT INTO executions VALUES (?,?,?,?)", executions)
        con.commit(); con.close()

    def hjob(self, name, **values):
        return {"id": name, "name": name, "enabled": True, "state": "scheduled",
                "schedule": {"display": "every day"}, **values}

    def test_hermes_error_missed_overdue_and_ok(self):
        future = (self.now + dt.timedelta(hours=1)).isoformat()
        past = (self.now - dt.timedelta(hours=1)).isoformat()
        self.hermes_files([
            self.hjob("error", last_status="error", last_error="bad\nsecond"),
            self.hjob("execmiss", next_run_at=future),
            self.hjob("overdue", next_run_at=past),
            self.hjob("good", last_status="ok", next_run_at=future),
        ], [("execmiss", "missed", past, past)])
        jobs = {j["id"]: j for j in scheduled_jobs.tick(self.cfg, self.now)["jobs"]}
        self.assertEqual({k: v["state"] for k, v in jobs.items()},
                         {"error": "failed", "execmiss": "missed", "overdue": "missed", "good": "ok"})
        self.assertEqual(jobs["error"]["detail"], "bad")

    def test_notifications_batch_once_recovery_and_retry(self):
        self.plist("one", StartInterval=60)
        self.plist("two", StartInterval=60)
        calls = []
        def notify(argv, **kwargs):
            from types import SimpleNamespace
            calls.append(argv[-1])
            return SimpleNamespace(returncode=0)
        self.cfg["scheduled_jobs"]["sources"] = ["launchd"]
        self.cfg["scheduled_jobs"]["notify_command"] = ["notify"]
        self.cfg["launchctl_runner"] = self.runner(0, 2)
        with patch.object(scheduled_jobs.subprocess, "run", side_effect=notify):
            self.tick(self.now, exit_code=2)
            self.tick(self.now + dt.timedelta(minutes=1), exit_code=2)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].count("FAILED"), 2)
            self.cfg["launchctl_runner"] = self.runner(1, 0)
            self.tick(self.now + dt.timedelta(minutes=2))
            self.assertEqual(len(calls), 2)
            self.assertIn("RECOVERED", calls[-1])

        # A failed send is retried on the next tick.
        self.state.joinpath("scheduled_jobs.sqlite").unlink()
        attempts = []
        def fail_then_succeed(argv, **kwargs):
            from types import SimpleNamespace
            attempts.append(argv[-1]); return SimpleNamespace(returncode=1 if len(attempts) == 1 else 0)
        with patch.object(scheduled_jobs.subprocess, "run", side_effect=fail_then_succeed):
            self.tick(self.now, exit_code=4)
            self.tick(self.now + dt.timedelta(minutes=1), exit_code=4)
        self.assertEqual(len(attempts), 2)

    def test_disabled_dashboard_state_is_none(self):
        from agentsmon import dashboard
        cfg = {"scheduled_jobs": {"enabled": False}}
        with patch.object(config, "load", return_value=cfg), \
             patch.object(dashboard, "_agents_state", return_value=[]), \
             patch.object(dashboard.plugins, "collect", return_value={"hidden_sessions": [], "patches": [],
                 "tables": [], "columns": [], "errors": [], "plugins": [], "loading": False}), \
             patch.object(dashboard, "_service_state", return_value=[]), \
             patch("agentsmon.integrity.status", return_value={"version": "test", "state": "clean"}):
            self.assertIsNone(json.loads(dashboard._state())["scheduled_jobs"])


if __name__ == "__main__":
    unittest.main()
