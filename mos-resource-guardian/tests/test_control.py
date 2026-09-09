import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control import validate, forecast, plan, Adapter
from engine import Engine
from service import Application, atomic_config


def target(**kw):
    return dict(kind="vm", id="test", cpu_min=1, cpu_max=4, priority=50,
                manage_memory=True, cpu_method="quota", memory_min_mib=2048,
                memory_max_mib=8192, memory_headroom_mib=512, **kw)


def snapshot():
    return dict(identity="boot:uuid:1", cpu=4, raw_cpu=[100000, 400000],
                memory=8192, raw_memory=8192, used=4096, memory_safe=True,
                cpu_ceiling=8, hotplug_safe=True)


def record(**kw):
    r = dict(timestamp=time.time(), cpu_percent=99, ram_available_mib=12000,
             ram_total_mib=32000, ram_reserve_mib=3200, reasons=["cpu_reserve"], state="pressure")
    r.update(kw)
    return r


class FakeAdapter:
    def __init__(self):
        self.s = snapshot()
        self.calls = []
        self.fail = False
        self.delay = False

    def snapshot(self, _):
        return copy.deepcopy(self.s)

    def apply(self, t, s, resource, value, raw=False):
        self.calls.append((resource, value, raw))
        if self.fail:
            raise RuntimeError("backend failed")
        if self.delay:
            return
        if resource == "cpu":
            self.s["cpu"] = value[1]/value[0] if raw else value
            self.s["raw_cpu"] = value if raw else [100000, int(value*100000)]
        else:
            self.s["memory"] = self.s["raw_memory"] = value


class Configuration(unittest.TestCase):
    def test_rejects_invalid_modes_targets_and_limits(self):
        cases = [{"mode": "force"}, {"targets": [{}]}, {"targets": [dict(target(), id="--all")]},
                 {"targets": [dict(target(), id="x;touch /tmp/x")]},
                 {"targets": [dict(target(), cpu_min=8)]},
                 {"targets": [dict(target(), manage_memory="true")]},
                 {"targets": [dict(target(), cpu_min=float("nan"))]},
                 {"targets": [target(), target()]}, {"data_directory": "/boot/history"},
                 {"monitor": {"cpu_reserve_percent": 99}}, {"forecast_samples": 6.5}]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                validate(case)

    def test_atomic_invalid_config_preserves_previous(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"settings.json"
            atomic_config(p, {})
            old = p.read_text()
            with self.assertRaises(ValueError):
                atomic_config(p, {"mode": "oops"})
            self.assertEqual(old, p.read_text())
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.c = validate({"targets": [target()]})
        self.s = snapshot()

    def test_cpu_step_and_floor(self):
        self.assertEqual(plan(target(), self.s, record(), self.c, {}), ("cpu", 3.5))
        self.s["cpu"] = 1
        self.assertIsNone(plan(target(), self.s, record(), self.c, {}))

    def test_no_balloon_without_fresh_stats(self):
        self.s["memory_safe"] = False
        self.assertIsNone(plan(target(), self.s, record(reasons=["ram_reserve"]), self.c, {}))

    def test_memory_guest_headroom(self):
        self.s["used"] = 8000
        self.assertIsNone(plan(target(), self.s, record(reasons=["ram_reserve"]), self.c, {}))
        self.s["used"] = 4096
        self.assertEqual(plan(target(), self.s, record(reasons=["ram_reserve"]), self.c, {}), ("memory", 7936))

    def test_ram_growth_never_spends_expected_release(self):
        self.s["memory"] = 4096
        low = record(state="normal", reasons=[], cpu_percent=5, ram_available_mib=3300)
        self.assertIsNone(plan(target(), self.s, low, self.c, {}))
        high = dict(low, ram_available_mib=10000)
        self.assertEqual(plan(target(), self.s, high, self.c, {}), ("memory", 4352))

    def test_unknown_cpu_never_actuates(self):
        self.assertIsNone(plan(target(), self.s, record(cpu_percent=None), self.c, {}))

    def test_conservative_profile_does_not_regrow(self):
        self.c["profile"] = "conservative"
        self.s["cpu"] = 1
        self.assertIsNone(plan(target(), self.s, record(state="normal", reasons=[], cpu_percent=5), self.c, {}))

    def test_forecast_learning_and_gaps(self):
        self.assertFalse(forecast([], self.c)["ready"])
        rows = [record(timestamp=100+i*5, cpu_percent=30+i*3) for i in range(12)]
        self.assertEqual(forecast(rows, self.c)["profile"], "conservative")
        rows[-1]["timestamp"] += 100
        self.assertFalse(forecast(rows, self.c)["ready"])

    def test_forecast_can_preempt_but_not_in_fixed_profile(self):
        prediction = dict(ready=True, profile="conservative", cpu_percent=99, ram_available_mib=12000)
        normal = record(state="normal", reasons=[], cpu_percent=20)
        self.assertEqual(plan(target(), self.s, normal, self.c, prediction), ("cpu", 3.5))
        self.c["profile"] = "balanced"
        self.assertIsNone(plan(target(), self.s, normal, self.c, prediction))


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.a = FakeAdapter()
        self.e = Engine(self.db, self.a)
        self.e.last_action = 0
        self.cfg = validate({"mode": "automatic", "targets": [target()]})

    def tearDown(self):
        self.db.close()

    def test_observe_does_not_write(self):
        self.cfg["mode"] = "observe"
        self.e.tick(self.cfg, record(), [])
        self.assertEqual(self.a.calls, [])
        self.assertEqual(self.e.inventory[0]["proposed"], ("cpu", 3.5))

    def test_apply_verify_and_cooldown(self):
        self.e.tick(self.cfg, record(), [])
        self.assertEqual(len(self.a.calls), 1)
        self.assertEqual(self.e.audit()[0]["status"], "pending")
        self.e.tick(self.cfg, record(), [])
        self.assertEqual(self.e.audit()[0]["status"], "verified")
        self.assertEqual(len(self.a.calls), 1)

    def test_failure_blocks_all_further_changes(self):
        self.a.fail = True
        self.e.tick(self.cfg, record(), [])
        self.e.last_action = 0
        self.e.tick(self.cfg, record(), [])
        self.assertEqual(len(self.a.calls), 1)
        self.assertEqual(self.e.unresolved(), 1)

    def test_crash_intent_is_blocked_after_restart(self):
        self.e.send(target(), self.a.s, "cpu", 3)
        restarted = Engine(self.db, self.a)
        restarted.last_action = 0
        restarted.tick(self.cfg, record(), [])
        self.assertEqual(len(self.a.calls), 1)

    def test_restore_original_and_preserve_external_change(self):
        self.e.send(target(), self.a.s, "cpu", 3)
        self.e.check_pending()
        self.e.restore_one(record())
        self.e.check_pending()
        self.assertEqual(self.a.s["cpu"], 4)
        self.assertEqual(self.e.unresolved(), 0)
        self.e.send(target(), self.a.s, "cpu", 2)
        self.e.check_pending()
        self.a.s["cpu"] = 3.2
        self.a.s["raw_cpu"] = [100000, 320000]
        self.e.restore_one(record())
        self.assertEqual(self.a.s["cpu"], 3.2)
        self.assertEqual(self.e.audit()[0]["status"], "conflict")

    def test_restart_expires_old_restore(self):
        self.e.send(target(), self.a.s, "cpu", 3)
        self.e.check_pending()
        self.a.s["identity"] = "different-boot:uuid:1"
        self.e.restore_one(record())
        self.assertEqual(self.e.audit()[0]["status"], "expired")

    def test_async_timeout_blocks(self):
        self.a.delay = True
        self.e.send(target(), self.a.s, "cpu", 3)
        row, entry, started = self.e.pending
        self.e.pending = (row, entry, started-31)
        self.e.check_pending()
        self.assertEqual(self.e.audit()[0]["status"], "failed")

    def test_priority_sheds_low_first(self):
        self.cfg["targets"] = [dict(target(), id="high", priority=100), dict(target(), id="low", priority=1)]
        # Distinct runtime identities, independent of configured aliases.
        self.a.snapshot = lambda t: dict(snapshot(), identity=t["id"])
        self.e.tick(self.cfg, record(), [])
        self.assertEqual(self.e.audit()[0]["target"], "vm:low")


class AdapterTests(unittest.TestCase):
    def test_container_writes_soft_memory_and_quota_only(self):
        with tempfile.TemporaryDirectory() as directory:
            a = Adapter()
            before = dict(snapshot(), path=directory, raw_memory='8589934592', raw_cpu=['400000', '100000'])
            t = dict(target(), kind="docker")
            with patch.object(a, "snapshot", return_value=before):
                a.apply(t, before, "memory", 7000)
                a.apply(t, before, "cpu", 2)
            self.assertEqual((Path(directory)/"memory.high").read_text(), str(7000*1048576))
            self.assertEqual((Path(directory)/"cpu.max").read_text(), '200000 100000')
            self.assertFalse((Path(directory)/"memory.max").exists())

    def test_container_memory_rechecks_current_usage(self):
        a = Adapter()
        before = snapshot()
        with patch.object(a, "snapshot", return_value=dict(before, used=7900)), self.assertRaises(ValueError):
            a.apply(dict(target(), kind="lxc"), before, "memory", 7936)

    def test_external_edit_refused_before_write(self):
        a = Adapter()
        before = snapshot()
        with patch.object(a, "snapshot", return_value=dict(before, raw_cpu=[100000, 1])):
            with self.assertRaisesRegex(ValueError, "externally"):
                a.apply(target(), before, "cpu", 2)

    def test_vm_calls_explicit_units_live_and_fixed_uri(self):
        calls = []
        a = Adapter(lambda cmd: calls.append(cmd) or "")
        before = snapshot()
        with patch.object(a, "snapshot", return_value=before):
            a.apply(target(), before, "memory", 7000)
        self.assertEqual(calls[0], ["virsh", "--connect", "qemu:///system", "setmem", "test", "7000MiB", "--live"])

    def test_hotunplug_fails_without_declared_capability(self):
        a = Adapter()
        before = dict(snapshot(), hotplug_safe=False)
        with patch.object(a, "snapshot", return_value=before), self.assertRaises(ValueError):
            a.apply(dict(target(), cpu_method="hotplug"), before, "cpu", 3)


if __name__ == "__main__":
    unittest.main()
