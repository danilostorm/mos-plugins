#!/usr/bin/env python3
"""Read-only host resource guardian. No workload mutations."""
import argparse
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time

DEFAULTS = dict(interval_seconds=5, retention_days=7, ram_reserve_mib=2048,
                ram_reserve_percent=10, emergency_ram_mib=1024,
                cpu_reserve_percent=15, recovery_cpu_percent=5,
                recovery_ram_mib=512, pressure_samples=3, recovery_samples=6)


def config(path=None):
    c = DEFAULTS.copy()
    if path:
        data = json.loads(Path(path).read_text())
        if not isinstance(data, dict) or set(data) - set(c):
            raise ValueError("Unknown configuration keys or invalid object")
        c.update(data)
    for key, value in c.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(key + " must be numeric")
        if not math.isfinite(value) or value <= 0:
            raise ValueError(key + " must be finite and positive")
    for key in ("pressure_samples", "recovery_samples"):
        if not isinstance(c[key], int):
            raise ValueError(key + " must be integer")
    for key in ("ram_reserve_percent", "cpu_reserve_percent", "recovery_cpu_percent"):
        if c[key] >= 100:
            raise ValueError(key + " must be below 100")
    if c["cpu_reserve_percent"] + c["recovery_cpu_percent"] >= 100:
        raise ValueError("CPU recovery margin is impossible")
    if c["emergency_ram_mib"] >= c["ram_reserve_mib"]:
        raise ValueError("Emergency RAM must be below reserve")
    if c["interval_seconds"] < 1 or c["retention_days"] > 365:
        raise ValueError("Interval >= 1 second; retention <= 365 days")
    return c


def cpu_counters(raw):
    row = raw.splitlines()[0].split()
    if row[0] != "cpu" or len(row) < 5:
        raise ValueError("Invalid aggregate CPU counters")
    ticks = list(map(int, row[1:9]))
    return sum(ticks), ticks[3] + (ticks[4] if len(ticks) > 4 else 0)


def memory(raw):
    fields = {}
    for row in raw.splitlines():
        parts = row.split()
        if parts[0] in ("MemTotal:", "MemAvailable:"):
            if len(parts) != 3 or parts[2] != "kB":
                raise ValueError("Invalid memory units")
            fields[parts[0][:-1]] = int(parts[1]) / 1024
    total, available = fields["MemTotal"], fields["MemAvailable"]
    if not 0 <= available <= total or total <= 0:
        raise ValueError("Invalid memory counters")
    return total, available


class Monitor:
    def __init__(self):
        self.previous = None

    def sample(self):
        current = cpu_counters(Path("/proc/stat").read_text())
        total, available = memory(Path("/proc/meminfo").read_text())
        cpu = None
        if self.previous:
            delta = current[0] - self.previous[0]
            idle = current[1] - self.previous[1]
            if delta > 0 and 0 <= idle <= delta:
                cpu = 100 * (1 - idle / delta)
        self.previous = current
        return dict(timestamp=time.time(), cpu_percent=cpu,
                    ram_total_mib=total, ram_available_mib=available)


class Policy:
    def __init__(self, cfg):
        self.c = cfg
        self.state = "warming_up"
        self.bad = self.good = 0

    def evaluate(self, s):
        c = self.c
        reserve = max(c["ram_reserve_mib"],
                      s["ram_total_mib"] * c["ram_reserve_percent"] / 100)
        available, cpu = s["ram_available_mib"], s["cpu_percent"]
        reasons = []
        if available < reserve:
            reasons.append("ram_reserve")
        if cpu is not None and cpu > 100 - c["cpu_reserve_percent"]:
            reasons.append("cpu_reserve")
        if available < c["emergency_ram_mib"]:
            self.state = "emergency"
            self.bad = self.good = 0
            reasons.append("ram_emergency")
        elif cpu is None:
            self.good = 0
        elif reasons:
            self.bad += 1
            self.good = 0
            if self.bad >= c["pressure_samples"] and self.state != "emergency":
                self.state = "pressure"
        else:
            self.bad = 0
            clear = (available >= reserve + c["recovery_ram_mib"] and
                     cpu <= 100 - c["cpu_reserve_percent"] - c["recovery_cpu_percent"])
            self.good = self.good + 1 if clear else 0
            if self.good >= c["recovery_samples"]:
                self.state = "normal"
        return dict(s, state=self.state, reasons=reasons,
                    ram_reserve_mib=reserve, mode="observe",
                    interval_seconds=c["interval_seconds"])


class History:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS samples (ts REAL, payload TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (ts REAL, state TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS events_ts ON events(ts)")
        self.db.commit()

    def add(self, record, retention_days):
        with self.db:
            last = self.db.execute("SELECT state FROM events ORDER BY ts DESC LIMIT 1").fetchone()
            if not last or last[0] != record["state"]:
                self.db.execute("INSERT INTO events VALUES (?, ?)",
                                (record["timestamp"], record["state"]))
                logging.warning("State: %s, reasons: %s", record["state"], record["reasons"])
            self.db.execute("INSERT INTO samples VALUES (?, ?)",
                            (record["timestamp"], json.dumps(record)))
            cutoff = record["timestamp"] - retention_days * 86400
            self.db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))

    def close(self):
        self.db.close()


def read_history(path, limit):
    # Read-only: status never creates an empty database.
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        return [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM samples ORDER BY ts DESC LIMIT ?", (limit,))]
    finally:
        db.close()


def run(path, cfg):
    os.umask(0o077)
    Path(path).resolve().parent.mkdir(parents=True, exist_ok=True)
    # Never unlink lockfile: unlinking permits parallel locks on separate inodes.
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        history = History(path)
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        monitor, policy = Monitor(), Policy(cfg)
        try:
            while not stop.is_set():
                started = time.monotonic()
                history.add(policy.evaluate(monitor.sample()), cfg["retention_days"])
                stop.wait(max(0, cfg["interval_seconds"] - (time.monotonic() - started)))
        finally:
            history.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--database", default="guardian.db")
    parser.add_argument("command", choices=["run", "status", "history"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        if args.command == "run":
            run(args.database, config(args.config))
        else:
            records = read_history(args.database, 1 if args.command == "status" else 100)
            if args.command == "status":
                record = records[0] if records else {}
                age = time.time() - record.get("timestamp", 0)
                record["stale"] = age < 0 or age > max(30, record.get("interval_seconds", 5) * 3)
                print(json.dumps(record))
            else:
                print(json.dumps(records))
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        logging.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
