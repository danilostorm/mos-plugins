"""Bounded, opt-in workload control and short-horizon prediction."""
import copy
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from guardian import config as monitor_config

DEFAULT = dict(mode="observe", profile="automatic", cooldown_seconds=60,
               forecast_seconds=60, forecast_samples=12, targets=[], monitor={},
               data_directory="/var/lib/mos-resource-guardian", auto_vms=False, auto_vm_exclude=[], free_affinity=[])


def number(v, lo, hi, key):
    if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or not lo <= v <= hi:
        raise ValueError(f"{key}: expected number between {lo} and {hi}")


def validate(data):
    if not isinstance(data, dict) or set(data) - set(DEFAULT):
        raise ValueError("Unknown configuration fields")
    c = dict(copy.deepcopy(DEFAULT), **copy.deepcopy(data))
    if not isinstance(c['free_affinity'], list) or len(c['free_affinity']) > 32 or any(not isinstance(x, str) or not re.fullmatch(r'[0-9a-fA-F-]{36}', x) for x in c['free_affinity']):
        raise ValueError('Invalid affinity VM UUIDs')
    if type(c['auto_vms']) is not bool or not isinstance(c['auto_vm_exclude'], list) or any(not isinstance(x, str) or not re.fullmatch(r'[0-9a-fA-F-]{36}', x) for x in c['auto_vm_exclude']):
        raise ValueError('Invalid automatic VM settings')
    directory = c["data_directory"]
    if not isinstance(directory, str) or not directory.startswith(("/mnt/", "/var/lib/")) or ".." in Path(directory).parts:
        raise ValueError("Data directory must be below /mnt or /var/lib")
    if c["mode"] not in ("observe", "automatic", "paused"):
        raise ValueError("Invalid mode")
    if c["profile"] not in ("automatic", "balanced", "conservative"):
        raise ValueError("Invalid profile")
    number(c["cooldown_seconds"], 30, 3600, "cooldown")
    number(c["forecast_seconds"], 10, 300, "forecast horizon")
    number(c["forecast_samples"], 6, 120, "forecast samples")
    if not isinstance(c["forecast_samples"], int):
        raise ValueError("forecast_samples must be integer")
    # Reuse numeric validation without writing a temporary file.
    m = monitor_config()
    if not isinstance(c["monitor"], dict) or set(c["monitor"]) - set(m):
        raise ValueError("Invalid monitor configuration")
    m.update(c["monitor"])
    for k, v in m.items():
        number(v, 0.001, 100000000, k)
    for k in ("pressure_samples", "recovery_samples"):
        if not isinstance(m[k], int) or not 1 <= m[k] <= 120:
            raise ValueError(k + " must be integer 1..120")
    if not 1 <= m["interval_seconds"] <= 60 or not 1 <= m["retention_days"] <= 365:
        raise ValueError("Invalid interval or retention")
    if not 0 < m["ram_reserve_percent"] < 90 or m["cpu_reserve_percent"] + m["recovery_cpu_percent"] >= 95:
        raise ValueError("Invalid percentage reserve")
    if m["emergency_ram_mib"] >= m["ram_reserve_mib"]:
        raise ValueError("Emergency RAM must be below reserve")
    c["monitor"] = m
    if not isinstance(c["targets"], list) or len(c["targets"]) > 32:
        raise ValueError("At most 32 targets")
    seen = set()
    for t in c["targets"]:
        allowed = {"kind", "id", "priority", "cpu_min", "cpu_max", "memory_min_mib", "memory_max_mib", "memory_headroom_mib", "manage_memory", "cpu_method", "lxc_path", "memory_autoscale"}
        if not isinstance(t, dict) or set(t) - allowed:
            raise ValueError("Invalid target fields")
        if t.get("kind") not in ("vm", "docker", "lxc") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", t.get("id", "")):
            raise ValueError("Invalid target identity")
        key = t["kind"] + ":" + t["id"]
        if key in seen:
            raise ValueError("Duplicate target")
        seen.add(key)
        t.setdefault("priority", 50)
        t.setdefault("manage_memory", False)
        t.setdefault('memory_autoscale', False)
        if type(t['memory_autoscale']) is not bool:
            raise ValueError('Invalid autoscale flag')
        t.setdefault("cpu_method", "quota")
        t.setdefault("memory_headroom_mib", 512)
        if type(t["manage_memory"]) is not bool or t["cpu_method"] not in ("quota", "hotplug"):
            raise ValueError("Invalid memory flag or CPU method")
        if t["cpu_method"] == "hotplug" and t["kind"] != "vm":
            raise ValueError("Hotplug is VM-only")
        for k, lo, hi in (("priority", 1, 100), ("cpu_min", .1, 4096), ("cpu_max", .1, 4096), ("memory_headroom_mib", 128, 1048576)):
            number(t.get(k), lo, hi, k)
        if t["cpu_min"] > t["cpu_max"]:
            raise ValueError("CPU min > max")
        if t["cpu_method"] == "hotplug" and any(int(t[k]) != t[k] for k in ("cpu_min", "cpu_max")):
            raise ValueError("Hotplug requires integer CPU counts")
        if t["manage_memory"]:
            for k in ("memory_min_mib", "memory_max_mib"):
                number(t.get(k), 256, 16777216, k)
            if t["memory_min_mib"] > t["memory_max_mib"]:
                raise ValueError("Memory min > max")
        if "lxc_path" in t:
            p = Path(t["lxc_path"])
            if t["kind"] != "lxc" or not p.is_absolute() or ".." in p.parts or str(p) == "/":
                raise ValueError("Invalid LXC path")
    return c


def command(args):
    # Fixed executables and argv: configuration can never supply a shell command.
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=8,
                          env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}).stdout.strip()


def pairs(raw):
    result = {}
    for line in raw.splitlines():
        row = line.replace(":", " ").split()
        if len(row) == 2:
            result[row[0]] = row[1]
    return result


class Adapter:
    def __init__(self, runner=command):
        self.run = runner

    def virsh(self, t, *args):
        return self.run(["virsh", "--connect", "qemu:///system", *args[:1], t["id"], *args[1:]])

    def snapshot(self, t):
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if t["kind"] == "vm":
            if self.virsh(t, "domstate").strip() != "running":
                raise ValueError("VM not running")
            xml = ET.fromstring(self.virsh(t, "dumpxml"))
            identity = boot + ":" + self.virsh(t, "domuuid") + ":" + self.virsh(t, "domid")
            cpus = int(self.virsh(t, "vcpucount", "--live", "--active"))
            sched = pairs(self.virsh(t, "schedinfo")) if t["cpu_method"] == "quota" else {}
            if t["cpu_method"] == "quota":
                period, quota = int(sched["global_period"]), int(sched["global_quota"])
                cpu = cpus if quota < 0 else quota / period
                raw_cpu = [period, quota]
            else:
                cpu, raw_cpu = cpus, cpus
            stats = pairs(self.virsh(t, "dommemstat")) if t["manage_memory"] else {}
            mem = int(stats.get("actual", 0)) / 1024
            age = time.time() - int(stats.get("last_update", 0))
            balloon = xml.find("./devices/memballoon")
            safe = (balloon is not None and balloon.get("model") == "virtio" and
                    xml.find("./memoryBacking/hugepages") is None and xml.find("./memoryBacking/locked") is None and
                    0 <= age <= 30 and "usable" in stats and mem > 0)
            usable = int(stats.get("usable", 0)) / 1024
            hotplug = xml.findall("./vcpus/vcpu")
            can_remove = bool(hotplug) and all(v.get("hotpluggable") == "yes" for v in hotplug if int(v.get("id", "0")) >= cpus - 1)
            return dict(identity=identity, cpu=cpu, raw_cpu=raw_cpu, memory=mem,
                        raw_memory=mem, memory_safe=safe, used=max(0, mem-usable),
                        hotplug_safe=can_remove, cpu_ceiling=int(xml.findtext("vcpu", str(cpus))))
        if t["kind"] == "docker":
            info = json.loads(self.run(["docker", "inspect", t["id"]]))[0]
            if not info["State"]["Running"]:
                raise ValueError("Container not running")
            pid = int(info["State"]["Pid"])
            identity = info["Id"]
        else:
            args = ["lxc-info", "-n", t["id"]]
            if t.get("lxc_path"):
                args += ["-P", t["lxc_path"]]
            pid = int(self.run(args + ["-pH"]))
            identity = t["id"]
        if pid <= 1:
            raise ValueError("Invalid container PID")
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        rows = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        cg = next((x[3:] for x in rows if x.startswith("0::")), None)
        if not cg or cg == "/":
            raise ValueError("Dedicated cgroup v2 required")
        root = Path("/sys/fs/cgroup").resolve()
        path = (root / cg.lstrip("/")).resolve()
        if root not in path.parents or ".." in Path(cg).parts:
            raise ValueError("Unsafe cgroup path")
        # Refuse the host's own cgroup and ancestors (nested Docker/LXC too).
        own = next(x[3:] for x in Path("/proc/self/cgroup").read_text().splitlines() if x.startswith("0::"))
        ownpath = (root / own.lstrip("/")).resolve()
        if path == ownpath or path in ownpath.parents:
            raise ValueError("Target contains guardian/host")
        quota, period = (path / "cpu.max").read_text().split()
        raw_memory = (path / "memory.high").read_text().strip()
        maximum = (path / "memory.max").read_text().strip()
        effective = (path / "cpuset.cpus.effective").read_text().strip()
        ceiling = sum(int(x.split("-")[-1])-int(x.split("-")[0])+1 for x in effective.split(","))
        cpu = ceiling if quota == "max" else int(quota) / int(period)
        used = int((path / "memory.current").read_text()) / 1048576
        memory = float("inf") if raw_memory == "max" else int(raw_memory) / 1048576
        return dict(identity=f"{boot}:{identity}:{pid}:{stat}:{path.stat().st_ino}", path=str(path), cpu=cpu,
                    raw_cpu=[quota, period], memory=memory, raw_memory=raw_memory,
                    used=used, memory_safe=True, cpu_ceiling=ceiling,
                    memory_ceiling=float("inf") if maximum == "max" else int(maximum)/1048576)

    def apply(self, t, snapshot, resource, value, raw=False):
        fresh = self.snapshot(t)
        if fresh["identity"] != snapshot["identity"]:
            raise ValueError("Workload restarted; refusing stale action")
        if fresh["raw_"+resource] != snapshot["raw_"+resource]:
            raise ValueError("Resource changed externally; refusing stale action")
        if resource == "memory" and not raw:
            if not fresh["memory_safe"] or (value < fresh['memory'] and value < fresh["used"] + t["memory_headroom_mib"]):
                raise ValueError("Fresh memory headroom check failed")
        if t["kind"] == "vm":
            if resource == "cpu":
                if t["cpu_method"] == "hotplug":
                    if value < fresh["cpu"] and not fresh["hotplug_safe"]:
                        raise ValueError("vCPU unplug capability not declared")
                    self.virsh(t, "setvcpus", str(int(value)), "--live")
                else:
                    period, quota = value if raw else (100000, int(value * 100000))
                    self.virsh(t, "schedinfo", "--set", f"global_period={period}", "--set", f"global_quota={quota}", "--live")
            else:
                self.virsh(t, "setmem", str(int(value)) + "MiB", "--live")
        else:
            path = Path(fresh["path"])
            if resource == "cpu":
                text = " ".join(map(str, value)) if raw else f"{int(value * 100000)} 100000"
                (path / "cpu.max").write_text(text)
            else:
                (path / "memory.high").write_text(str(value) if raw else str(int(value * 1048576)))


def forecast(samples, cfg):
    rows = [s for s in samples if s.get("cpu_percent") is not None][-120:]
    if len(rows) < cfg["forecast_samples"]:
        return dict(ready=False, profile="balanced", reason="learning")
    rows = rows[-cfg["forecast_samples"]:]
    times = [r["timestamp"] for r in rows]
    if any(b <= a or b-a > cfg["monitor"]["interval_seconds"] * 3 for a, b in zip(times, times[1:])):
        return dict(ready=False, profile="balanced", reason="history_gap")
    xs = [t-times[0] for t in times]
    meanx = sum(xs)/len(xs)
    var = sum((x-meanx)**2 for x in xs)
    def predict(key):
        ys = [s[key] for s in rows]
        mean = sum(ys)/len(ys)
        slope = sum((x-meanx)*(y-mean) for x, y in zip(xs, ys))/var
        return mean + slope*(xs[-1]+cfg["forecast_seconds"]-meanx)
    cpu = max(0, min(100, predict("cpu_percent")))
    ram = max(0, min(rows[-1]["ram_total_mib"], predict("ram_available_mib")))
    reserve = max(cfg["monitor"]["ram_reserve_mib"], rows[-1]["ram_total_mib"]*cfg["monitor"]["ram_reserve_percent"]/100)
    risky = cpu > 100-cfg["monitor"]["cpu_reserve_percent"] or ram < reserve
    return dict(ready=True, cpu_percent=round(cpu, 2), ram_available_mib=round(ram, 2),
                profile="conservative" if risky else "balanced", horizon_seconds=cfg["forecast_seconds"])


def plan(target, snap, record, cfg, prediction):
    """One resource per action; no optimistic credit for anticipated RAM release."""
    pressured = record["state"] in ("pressure", "emergency")
    predicted = cfg["profile"] == "automatic" and prediction.get("ready") and prediction["profile"] == "conservative"
    conservative = cfg["profile"] == "conservative" or predicted
    cpu_pressure = "cpu_reserve" in record["reasons"] or predicted and prediction["cpu_percent"] > 100-cfg["monitor"]["cpu_reserve_percent"]
    ram_pressure = "ram_reserve" in record["reasons"] or predicted and prediction["ram_available_mib"] < record["ram_reserve_mib"]
    if record["cpu_percent"] is None:
        return None
    if (pressured or predicted) and ram_pressure and target["manage_memory"] and snap["memory_safe"]:
        current = min(snap["memory"], target["memory_max_mib"])
        value = max(target["memory_min_mib"], math.ceil(snap["used"] + target["memory_headroom_mib"]), current-256)
        if value < snap["memory"] and value <= current:
            return "memory", value
    if (pressured and cpu_pressure) or predicted and cpu_pressure:
        step = 1 if target["cpu_method"] == "hotplug" else .5
        value = max(target["cpu_min"], min(target["cpu_max"], snap["cpu"])-step)
        if value < snap["cpu"]:
            return "cpu", value
    # Recovery is gradual and never adds RAM without actual free host capacity.
    if record["state"] == "normal" and not record["reasons"] and not conservative:
        if target.get('memory_autoscale') and target['manage_memory'] and snap['memory_safe']:
            free = snap['memory'] - snap['used']
            ceiling = min(target['memory_max_mib'], snap.get('memory_ceiling', float('inf')))
            headroom = target['memory_headroom_mib']
            if free < headroom and snap['memory'] < ceiling:
                increase = min(256, ceiling-snap['memory'])
                if record['ram_available_mib']-increase >= record['ram_reserve_mib']+cfg['monitor']['recovery_ram_mib']:
                    return 'memory', snap['memory']+increase
            if free > 2*headroom+256:
                value = max(target['memory_min_mib'], snap['memory']-256)
                if value < snap['memory']:
                    return 'memory', value
        if snap["cpu"] < min(target["cpu_max"], snap["cpu_ceiling"]):
            step = 1 if target["cpu_method"] == "hotplug" else .5
            cpu_budget = max(0, (100-cfg["monitor"]["cpu_reserve_percent"]-cfg["monitor"]["recovery_cpu_percent"]-record["cpu_percent"])/100*(os.cpu_count() or 1))
            if cpu_budget >= step:
                return "cpu", min(target["cpu_max"], snap["cpu_ceiling"], snap["cpu"]+step)
        if target["manage_memory"] and snap["memory_safe"] and not target.get('memory_autoscale'):
            ceiling = min(target["memory_max_mib"], snap.get("memory_ceiling", float("inf")))
            if snap["memory"] < ceiling and record["ram_available_mib"] - 256 >= record["ram_reserve_mib"] + cfg["monitor"]["recovery_ram_mib"]:
                return "memory", min(ceiling, snap["memory"]+256)
    return None
