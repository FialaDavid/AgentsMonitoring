"""Opt-in sampling and state derivation for time-triggered user jobs."""
from __future__ import annotations

import datetime as dt
import fnmatch
import json
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import subprocess

from . import config


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE IF NOT EXISTS samples (job_id TEXT, ts REAL, runs INTEGER, last_exit INTEGER)")
    con.execute("CREATE INDEX IF NOT EXISTS samples_job_ts ON samples(job_id, ts)")
    con.execute("CREATE TABLE IF NOT EXISTS notified (job_id TEXT PRIMARY KEY, state TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS failing (job_id TEXT PRIMARY KEY, since REAL)")
    return con


def _launchd_plist(path: Path):
    try:
        with path.open("rb") as f:
            return plistlib.load(f)
    except Exception:
        try:
            p = subprocess.run(["plutil", "-convert", "xml1", "-o", "-", str(path)],
                               capture_output=True, timeout=10)
            if p.returncode == 0:
                return plistlib.loads(p.stdout)
        except Exception:
            pass
    return None


def _launchd_status(label, runner):
    try:
        result = runner(["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)])
        rc = getattr(result, "returncode", 1)
        output = getattr(result, "stdout", "") or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        if rc:
            return None, None, "unloaded"
        runs = re.search(r"\bruns\s*=\s*(\d+)", output)
        exit_code = re.search(r"\blast exit code\s*=\s*(-?\d+)", output)
        return (int(runs.group(1)) if runs else None,
                int(exit_code.group(1)) if exit_code else None, None)
    except Exception:
        return None, None, "unloaded"


def _calendar_matches(value, when):
    if not isinstance(value, dict):
        return False
    fields = {"Minute": when.minute, "Hour": when.hour, "Day": when.day,
              "Weekday": (when.weekday() + 1) % 7, "Month": when.month}
    return all(key not in value or int(value[key]) == fields[key] for key in fields)


def _recent_calendar(intervals, now):
    candidates = []
    for days in range(0, 367):
        day = now.date() - dt.timedelta(days=days)
        for hour in range(23, -1, -1):
            for minute in range(59, -1, -1):
                when = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=now.tzinfo)
                if when > now:
                    continue
                if any(_calendar_matches(item, when) for item in intervals):
                    candidates.append(when.timestamp())
                    if len(candidates) >= 1:
                        return candidates[0]
    return None


def _samples(con, job_id):
    return con.execute("SELECT ts,runs,last_exit FROM samples WHERE job_id=? ORDER BY ts", (job_id,)).fetchall()


def _covered(rows, target, now, interval):
    before = [r for r in rows if r[0] < target]
    after = [r for r in rows if target <= r[0] <= now]
    if not before or not after:
        return False
    points = [r[0] for r in rows if before[-1][0] <= r[0] <= after[-1][0]]
    return all(b - a <= 3 * interval for a, b in zip(points, points[1:]))


def _launchd(cfg, con, now):
    jobs, errors = [], []
    directory = Path(os.path.expanduser(os.path.expandvars(cfg.get("launchd_dir", "~/Library/LaunchAgents"))))
    runner = cfg.get("launchctl_runner") or cfg.get("_launchctl_runner") or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=10))
    ignored = cfg.get("ignore", [])
    if not directory.is_dir():
        return jobs, errors
    for path in sorted(directory.glob("*.plist")):
        data = _launchd_plist(path)
        if not isinstance(data, dict):
            errors.append(str(path))
            continue
        calendar = data.get("StartCalendarInterval")
        intervals = calendar if isinstance(calendar, list) else [calendar] if isinstance(calendar, dict) else []
        start_interval = data.get("StartInterval")
        if data.get("KeepAlive") or (not intervals and not start_interval):
            continue
        label = str(data.get("Label") or path.stem)
        if any(fnmatch.fnmatchcase(label, pattern) for pattern in ignored):
            continue
        runs, last_exit, unloaded = _launchd_status(label, runner)
        rows = _samples(con, label)
        ts = now.timestamp()
        if runs is not None:
            con.execute("INSERT INTO samples VALUES (?,?,?,?)", (label, ts, runs, last_exit))
            rows = _samples(con, label)
        increases = []
        reset_ts = None
        previous = None
        for sample_ts, sample_runs, _ in rows:
            if previous is not None and sample_runs < previous:
                reset_ts = sample_ts
            elif previous is not None and sample_runs > previous:
                increases.append(sample_ts)
            previous = sample_runs
        last_run = increases[-1] if increases else None
        detail = ""
        if unloaded:
            state = "unloaded"
        elif isinstance(last_exit, int) and last_exit != 0:
            state, detail = "failed", "last exit code %d" % last_exit
        else:
            target = None
            if intervals:
                target = _recent_calendar(intervals, now)
            elif start_interval:
                base = increases[-1] if increases else (rows[0][0] if rows else None)
                if reset_ts is not None:
                    base = max(base, reset_ts) if base is not None else reset_ts
                if base is not None:
                    target = base + int(start_interval)
            if reset_ts is not None and target is not None and target < reset_ts:
                target = None
            missed = (target is not None and target <= ts - int(cfg.get("grace_seconds", 900))
                      and _covered(rows, target, ts, int(cfg.get("interval_seconds", 60)))
                      and not any(t > target - int(cfg.get("interval_seconds", 60)) for t in increases))
            if missed:
                state, detail = "missed", "expected fire was not observed"
            elif increases or last_exit == 0:
                state = "ok"
            else:
                state = "unknown"
        jobs.append({"source": "launchd", "id": label,
                     "schedule": "calendar" if intervals else "every %s seconds" % start_interval,
                     "state": state, "last_run_at": dt.datetime.fromtimestamp(last_run, now.tzinfo).isoformat() if last_run else None,
                     "last_exit": last_exit, "detail": detail})
    return jobs, errors


def _hermes(cfg, now):
    jobs, errors = [], []
    root = Path(os.path.expanduser(os.path.expandvars(cfg.get("hermes_cron_dir", "~/.hermes/cron"))))
    jobs_path, db_path = root / "jobs.json", root / "executions.db"
    try:
        data = json.loads(jobs_path.read_text("utf-8"))
    except (OSError, ValueError):
        return jobs, errors
    if not db_path.is_file():
        return jobs, errors
    latest = {}
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        try:
            columns = [r[1] for r in con.execute("PRAGMA table_info(executions)")]
            jid_col = next((c for c in ("job_id", "jobId", "id") if c in columns), None)
            if jid_col and "status" in columns:
                order = "scheduled_instant" if "scheduled_instant" in columns else ("finished_at" if "finished_at" in columns else "rowid")
                for jid, status in con.execute("SELECT %s,status FROM executions ORDER BY %s" % (jid_col, order)):
                    latest[jid] = status
        finally:
            con.close()
    except sqlite3.Error:
        pass
    for item in data.get("jobs", []):
        if not item.get("enabled") or item.get("state") != "scheduled":
            continue
        jid = str(item.get("name") or item.get("id") or "")
        if any(fnmatch.fnmatchcase(jid, pattern) for pattern in cfg.get("ignore", [])):
            continue
        detail, state = "", "unknown"
        if item.get("last_status") == "error":
            state = "failed"
            detail = str(item.get("last_error") or "").splitlines()[0][:200]
        else:
            next_run = item.get("next_run_at")
            overdue = False
            if next_run:
                try:
                    overdue = dt.datetime.fromisoformat(next_run) < now - dt.timedelta(seconds=int(cfg.get("grace_seconds", 900)))
                except (ValueError, TypeError):
                    pass
            if latest.get(item.get("id")) == "missed" or overdue:
                state, detail = "missed", "scheduled execution was missed"
            elif item.get("last_status") == "ok":
                state = "ok"
        jobs.append({"source": "hermes", "id": jid,
                     "schedule": (item.get("schedule") or {}).get("display") or (item.get("schedule") or {}).get("expr") or "",
                     "state": state, "last_run_at": item.get("last_run_at"), "last_exit": None, "detail": detail})
    return jobs, errors


def tick(cfg, now=None):
    """Collect, derive, persist and notify one scheduled-jobs sample."""
    current = now or dt.datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    settings = cfg.get("scheduled_jobs", cfg)
    root = config.state_dir()
    con = _connect(root / "scheduled_jobs.sqlite")
    try:
        con.execute("DELETE FROM samples WHERE ts < ?", (current.timestamp() - 14 * 86400,))
        jobs, errors = [], []
        sources = settings.get("sources", ["launchd", "hermes"])
        if "launchd" in sources:
            found, errs = _launchd({**settings, "launchctl_runner": cfg.get("launchctl_runner") or settings.get("launchctl_runner")}, con, current)
            jobs.extend(found); errors.extend(errs)
        if "hermes" in sources:
            found, errs = _hermes(settings, current)
            jobs.extend(found); errors.extend(errs)
        transitions = []
        for job in jobs:
            key = job["source"] + ":" + job["id"]
            state = job["state"]
            if state == "failed":
                con.execute("INSERT OR IGNORE INTO failing VALUES (?,?)", (key, current.timestamp()))
            else:
                con.execute("DELETE FROM failing WHERE job_id=?", (key,))
            previous = con.execute("SELECT state FROM notified WHERE job_id=?", (key,)).fetchone()
            prior_state = previous[0] if previous else None
            failure = con.execute("SELECT since FROM failing WHERE job_id=?", (key,)).fetchone()
            grace = int(settings.get("grace_seconds", 900))
            if state == "failed" and failure and current.timestamp() - failure[0] >= grace and state != prior_state:
                transitions.append((key, state, "%s  %s  %s" % (state.upper(), key, job["detail"])))
            elif state == "missed" and state != prior_state:
                transitions.append((key, state, "%s  %s  %s" % (state.upper(), key, job["detail"])))
            elif state == "ok" and prior_state in ("failed", "missed"):
                transitions.append((key, "ok", "RECOVERED  %s  recovered" % key))
        command = settings.get("notify_command", [])
        if transitions and command:
            message = "AgentsMon: scheduled jobs\n" + "\n".join(line for _, _, line in transitions)
            try:
                sent = subprocess.run(list(command) + [message], capture_output=True, timeout=30).returncode == 0
            except Exception:
                sent = False
            if sent:
                for key, state, _ in transitions:
                    con.execute("INSERT OR REPLACE INTO notified VALUES (?,?)", (key, state))
        con.commit()
    finally:
        con.close()
    output = {"updated_at": current.isoformat(), "jobs": jobs, "errors": errors}
    target = root / "scheduled_jobs.json"
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    temp.replace(target)
    return output
