"""Durable action journal, compare-before-write and asynchronous verification."""
import json
import math
import time
from control import Adapter, forecast, plan
from guardian import memory
from pathlib import Path


class Engine:
    def __init__(self, db, adapter=None):
        self.db, self.adapter = db, adapter or Adapter()
        db.execute("CREATE TABLE IF NOT EXISTS actions (id INTEGER PRIMARY KEY, ts REAL, target TEXT, payload TEXT, status TEXT, error TEXT)")
        db.commit()
        self.last_action = time.monotonic()  # Cooldown also applies after a restart.
        self.pending = None
        self.restore_requested = False
        self.inventory = []

    def audit(self, limit=100):
        return [dict(id=r[0], timestamp=r[1], target=r[2], action=json.loads(r[3]), status=r[4], error=r[5])
                for r in self.db.execute("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,))]

    def set_status(self, rowid, status, error=""):
        with self.db:
            self.db.execute("UPDATE actions SET status=?, error=? WHERE id=?", (status, error, rowid))

    def unresolved(self):
        return self.db.execute("SELECT count(*) FROM actions WHERE status IN ('intent','pending','conflict','failed')").fetchone()[0]

    def compact(self, entry):
        rows = self.db.execute("SELECT id,payload FROM actions WHERE status='verified' ORDER BY id").fetchall()
        matching = [i for i, raw in rows if self.lineage(json.loads(raw)) == self.lineage(entry)]
        for rowid in matching[1:-1]:
            self.set_status(rowid, "superseded")

    @staticmethod
    def lineage(entry):
        return (entry["identity"], entry["resource"], entry["spec"]["kind"],
                entry["spec"]["cpu_method"] if entry["resource"] == "cpu" else "memory")

    @staticmethod
    def equal(a, b):
        if isinstance(a, (float, int)) and isinstance(b, (float, int)):
            return abs(a-b) < .01
        return a == b

    def check_pending(self):
        if not self.pending:
            return
        rowid, entry, started = self.pending
        try:
            snap = self.adapter.snapshot(entry["spec"])
            if snap["identity"] != entry["identity"]:
                self.set_status(rowid, "expired", "Workload restarted")
                self.pending = None
                return
            value = snap["raw_" + entry["resource"]] if entry.get("restore") else snap[entry["resource"]]
            if self.equal(value, entry["after"]):
                self.set_status(rowid, "restored" if entry.get("restore") else "verified")
                if not entry.get("restore"):
                    self.compact(entry)
                if entry.get("restore"):
                    for parent in entry["parents"]:
                        self.set_status(parent, "restored")
                self.pending = None
            elif time.monotonic()-started > 30:
                self.set_status(rowid, "failed", "Not confirmed within 30 seconds; automatic changes blocked, request restoration")
                self.pending = None
        except Exception as exc:
            if time.monotonic()-started > 30:
                self.set_status(rowid, "failed", str(exc)[:300])
                self.pending = None

    def send(self, spec, snap, resource, value, restore=False, parents=None):
        entry = dict(spec=spec, identity=snap["identity"], resource=resource,
                     before=snap["raw_"+resource], after=value, restore=restore, parents=parents or [])
        with self.db:
            rowid = self.db.execute("INSERT INTO actions(ts,target,payload,status,error) VALUES (?,?,?,'intent','')",
                                    (time.time(), spec["kind"]+":"+spec["id"], json.dumps(entry),)).lastrowid
        self.last_action = time.monotonic()
        try:
            # Journal is committed before mutation. A crash blocks further writes.
            self.adapter.apply(spec, snap, resource, value, raw=restore)
            self.set_status(rowid, "pending")
            self.pending = (rowid, entry, time.monotonic())
        except Exception as exc:
            self.set_status(rowid, "failed", str(exc)[:300])

    def restore_one(self, record):
        rows = self.db.execute("SELECT id,payload FROM actions WHERE status IN ('intent','pending','conflict','failed','verified') ORDER BY id").fetchall()
        # Restore originals, not intermediate limits, and preserve other administrators' edits.
        if not rows:
            self.restore_requested = False
            return
        rowid, raw = rows[0]
        first = json.loads(raw)
        spec, resource = first["spec"], first["resource"]
        matching = [(i, json.loads(p)) for i, p in rows if self.lineage(json.loads(p)) == self.lineage(first)]
        try:
            snap = self.adapter.snapshot(spec)
            if snap["identity"] != first["identity"]:
                for i, _ in matching:
                    self.set_status(i, "expired", "Workload restarted; old limits not restored")
                return
            original = first["before"]
            if self.equal(snap["raw_"+resource], original):
                for i, _ in matching:
                    self.set_status(i, "restored")
                return
            latest = matching[-1][1]
            compare = snap["raw_"+resource] if latest.get("restore") else snap[resource]
            if not self.equal(compare, latest["after"]):
                self.set_status(rowid, "conflict", "Current value differs from last request; inspect and restore original manually")
                self.restore_requested = False
                return
            if spec["kind"] == "vm" and resource == "memory":
                increase = max(0, original-snap["memory"])
                if not snap["memory_safe"] or original < snap["used"]+spec["memory_headroom_mib"] or record["ram_available_mib"]-increase < record["ram_reserve_mib"]:
                    return  # Wait for actual host and guest capacity.
            self.send(spec, snap, resource, original, True, [i for i, _ in matching])
        except Exception as exc:
            self.set_status(rowid, "conflict", str(exc)[:300])
            self.restore_requested = False

    def tick(self, cfg, record, history):
        prediction = forecast(history, cfg)
        self.check_pending()
        if self.pending:
            return prediction
        if self.restore_requested:
            if time.monotonic()-self.last_action >= cfg["cooldown_seconds"]:
                self.restore_one(record)
            return prediction
        inventory, candidates, identities = [], [], set()
        duplicate = False
        ordered = sorted(cfg["targets"], key=lambda t: t["priority"], reverse=record["state"] == "normal")
        for t in ordered:
            key = t["kind"]+":"+t["id"]
            try:
                s = self.adapter.snapshot(t)
                if s["identity"] in identities:
                    duplicate = True
                    raise ValueError("Duplicate running workload identity")
                identities.add(s["identity"])
                candidate = plan(t, s, record, cfg, prediction)
                inventory.append(dict(target=key, cpu=s["cpu"], memory_mib=s["memory"] if math.isfinite(s["memory"]) else None,
                                      used_mib=s["used"], memory_safe=s["memory_safe"], proposed=candidate, error=None))
                if candidate:
                    candidates.append((t, s, *candidate))
            except Exception as exc:
                inventory.append(dict(target=key, error=str(exc)[:300]))
        self.inventory = inventory
        if cfg["mode"] == "automatic" and not duplicate and not self.unresolved() and candidates and time.monotonic()-self.last_action >= cfg["cooldown_seconds"]:
            target, snap, resource, value = candidates[0]
            if time.time()-record["timestamp"] > max(15, cfg["monitor"]["interval_seconds"]*3):
                return prediction
            if resource == "memory" and target["kind"] == "vm" and value > snap["memory"]:
                total, available = memory(Path("/proc/meminfo").read_text())
                reserve = max(cfg["monitor"]["ram_reserve_mib"], total*cfg["monitor"]["ram_reserve_percent"]/100)
                if available-(value-snap["memory"]) < reserve+cfg["monitor"]["recovery_ram_mib"]:
                    return prediction
            self.send(target, snap, resource, value)
        return prediction
