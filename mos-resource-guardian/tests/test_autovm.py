import copy
from pathlib import Path
import sys
import unittest
import sqlite3
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autovm import AutoVM, definition
from control import validate, plan
from engine import Engine

UUID = '61743f04-a5ca-4d04-8689-fed8c9e76964'
XML = f'''<domain type="kvm"><name>DANILO</name><uuid>{UUID}</uuid>
<memory unit="GiB">16</memory><vcpu>16</vcpu>
<devices><memballoon model="virtio"/></devices></domain>'''


class AutomaticVMTests(unittest.TestCase):
    def runner(self, args):
        self.calls.append(args)
        if 'list' in args:
            return UUID
        if 'dumpxml' in args:
            return self.xml
        if 'domid' in args:
            return '1'
        return ''

    def setUp(self):
        self.calls, self.xml = [], XML
        self.a = AutoVM(self.runner)
        self.cfg = validate(dict(auto_vms=True))

    def test_native_limits_and_safe_automatic_floor(self):
        t, item = definition(XML)
        self.assertEqual((t['cpu_max'], t['memory_max_mib'], t['memory_min_mib']), (16, 16384, 4096))
        self.assertEqual(t['id'], UUID)
        self.assertFalse(item['pinned'])

    def test_units_default_kib(self):
        xml = XML.replace('<memory unit="GiB">16', '<memory>16777216')
        self.assertEqual(definition(xml)[0]['memory_max_mib'], 16384)

    def test_pin_is_detected_and_never_rewritten(self):
        self.xml = XML.replace('<devices>', '<cputune><vcpupin vcpu="0" cpuset="2"/></cputune><devices>')
        self.a.resolve(self.cfg)
        self.assertTrue(self.a.catalog[0]['pinned'])
        self.assertFalse(any('define' in c or 'vcpupin' in c for c in self.calls))

    def test_hugepages_and_missing_balloon_cpu_only(self):
        for xml in (XML.replace('<devices>', '<memoryBacking><hugepages/></memoryBacking><devices>'), XML.replace('<memballoon model="virtio"/>', '')):
            self.assertFalse(definition(xml)[0]['manage_memory'])

    def test_read_saved_definition_not_balloon_actual(self):
        out = self.a.resolve(self.cfg)
        self.assertEqual(len(out['targets']), 1)
        self.assertIn('--inactive', self.calls[-1])
        self.assertEqual(self.cfg['targets'], [])
        self.xml = XML.replace('>16</vcpu>', '>8</vcpu>')
        self.a.last = -float('inf')
        self.assertEqual(self.a.resolve(self.cfg)['targets'][0]['cpu_max'], 8)

    def test_manual_override_by_name_and_exclusion(self):
        self.cfg['auto_vm_exclude'] = [UUID]
        self.assertEqual(self.a.resolve(self.cfg)['targets'], [])
        self.cfg['auto_vm_exclude'] = []
        t = definition(XML)[0]
        t['id'] = 'DANILO'
        self.cfg['targets'] = [t]
        self.assertEqual(len(self.a.resolve(self.cfg)['targets']), 1)

    def test_disabled_discovery_makes_no_calls(self):
        self.cfg['auto_vms'] = False
        self.a.resolve(self.cfg)
        self.assertEqual(self.calls, [])

    def test_failed_discovery_never_reuses_old_targets(self):
        self.a.resolve(self.cfg)
        self.a.last = -float('inf')
        def fail(_):
            raise RuntimeError('backend offline')
        self.a.run = fail
        self.assertEqual(self.a.resolve(self.cfg)['targets'], [])

    def test_observe_never_enables_balloon_polling(self):
        resolved = self.a.resolve(self.cfg)
        self.calls.clear()
        self.a.enable_stats(resolved)
        self.assertEqual(self.calls, [])
        resolved['mode'] = 'automatic'
        self.a.enable_stats(resolved)
        self.assertTrue(any('dommemstat' in c and '--live' in c for c in self.calls))
        self.assertFalse(any('--config' in c or 'define' in c for c in self.calls))

    def test_ram_tracks_guest_demand(self):
        t = definition(XML)[0]
        s = dict(cpu=16, cpu_ceiling=16, memory=8192, used=4096, memory_safe=True)
        r = dict(state='normal', reasons=[], cpu_percent=10, ram_available_mib=20000, ram_reserve_mib=6400)
        self.assertEqual(plan(t, s, r, self.cfg, {}), ('memory', 7936))
        s['used'] = 8000
        self.assertEqual(plan(t, s, r, self.cfg, {}), ('memory', 8448))
        r['ram_available_mib'] = 6500
        self.assertIsNone(plan(t, s, r, self.cfg, {}))

    def test_ram_deadband_no_oscillation_or_fill_to_max(self):
        t = definition(XML)[0]
        s = dict(cpu=16, cpu_ceiling=16, memory=8192, used=7400, memory_safe=True)
        r = dict(state='normal', reasons=[], cpu_percent=10, ram_available_mib=20000, ram_reserve_mib=6400)
        self.assertIsNone(plan(t, s, r, self.cfg, {}))

    def test_reject_bad_auto_settings(self):
        for c in ({'auto_vms': 'yes'}, {'auto_vm_exclude': ['--all']}):
            with self.assertRaises(ValueError):
                validate(c)

    def test_editor_change_blocks_stale_action_before_journal(self):
        t = definition(XML)[0]
        cfg = validate(dict(auto_vms=True, mode='automatic', targets=[t]))
        class Fake:
            def snapshot(self, _):
                return dict(identity=UUID, cpu=16, raw_cpu=[100000,1600000], memory=8192,
                            used=4000, memory_safe=True, cpu_ceiling=16)
            def virsh(self, *_):
                return XML.replace('>16</vcpu>', '>8</vcpu>')
            def apply(self, *_):
                raise AssertionError('Stale write')
        db = sqlite3.connect(':memory:')
        try:
            engine = Engine(db, Fake())
            engine.last_action = time.monotonic()-120
            r = dict(timestamp=time.time(), state='pressure', reasons=['cpu_reserve'], cpu_percent=99,
                     ram_available_mib=20000, ram_reserve_mib=6400)
            engine.tick(cfg, r, [])
            self.assertEqual(engine.audit(), [])
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
