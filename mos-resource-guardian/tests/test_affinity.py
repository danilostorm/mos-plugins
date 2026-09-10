import sys
from pathlib import Path
import sqlite3
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from affinity import Affinity, cpus
from control import validate

UUID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'


class AffinityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.pins = {'0': '0', '1': '1'}
        self.domain = '7'
        self.writes = []
        self.inject_failure = False
        self.a = Affinity(self.db, self.runner, boot='boot', online=lambda: '0-3')
        self.cfg = dict(auto_vms=True, auto_vm_exclude=[], free_affinity=[UUID],
                        mode='automatic', targets=[dict(kind='vm', id=UUID)])

    def tearDown(self):
        self.db.close()

    def runner(self, args):
        op = args[3]
        if op == 'domid':
            return self.domain
        self.assertEqual(op, 'vcpupin')
        self.assertEqual(args[-1], '--live')
        if len(args) == 6:
            return ' VCPU CPU Affinity\n----------------\n' + '\n'.join(k+' '+v for k, v in self.pins.items())
        # Original is durable before any mutation.
        self.assertEqual(self.db.execute('SELECT count(*) FROM affinity').fetchone()[0], 1)
        if self.inject_failure and args[5] == '1':
            raise ValueError('Injected failure')
        self.writes.append(args)
        self.pins[args[5]] = cpus(args[6])
        return ''

    def test_enable_disable(self):
        self.a.tick(self.cfg)
        self.assertEqual(self.pins, {'0': '0,1,2,3', '1': '0,1,2,3'})
        self.cfg['free_affinity'] = []
        self.a.tick(self.cfg)
        self.assertEqual(self.pins, {'0': '0', '1': '1'})
        self.assertEqual(self.db.execute('SELECT count(*) FROM affinity').fetchone()[0], 0)

    def test_daemon_restart_retains_originals(self):
        self.a.tick(self.cfg)
        self.a = Affinity(self.db, self.runner, boot='boot', online=lambda: '0-3')
        self.a.tick(self.cfg, restore=True)
        self.assertEqual(self.pins, {'0': '0', '1': '1'})

    def test_vm_restart_uses_new_originals(self):
        self.a.tick(self.cfg)
        self.domain = '8'
        self.pins = {'0': '2', '1': '3'}
        self.a.tick(self.cfg)
        self.cfg['free_affinity'] = []
        self.a.tick(self.cfg)
        self.assertEqual(self.pins, {'0': '2', '1': '3'})

    def test_observe_and_pause_do_not_release(self):
        for mode in ['observe', 'paused']:
            self.cfg['mode'] = mode
            self.a.tick(self.cfg)
        self.assertEqual(self.writes, [])

    def test_external_change_preserved(self):
        self.a.tick(self.cfg)
        self.pins['0'] = '2'
        self.cfg['free_affinity'] = []
        self.a.tick(self.cfg)
        self.assertEqual(self.pins['0'], '2')
        self.assertIn('externamente', self.a.status[0]['message'])

    def test_partial_failure_restores(self):
        self.inject_failure = True
        self.a.tick(self.cfg)
        self.inject_failure = False
        self.cfg['free_affinity'] = []
        self.a.tick(self.cfg)
        self.assertEqual(self.pins, {'0': '0', '1': '1'})

    def test_discovery_failure_does_not_restore(self):
        self.a.tick(self.cfg)
        self.cfg['targets'] = []
        self.a.tick(self.cfg)
        self.assertEqual(self.pins['0'], '0,1,2,3')

    def test_exclusion_restores_even_paused(self):
        self.a.tick(self.cfg)
        self.cfg.update(mode='paused', auto_vm_exclude=[UUID])
        self.a.tick(self.cfg)
        self.assertEqual(self.pins['0'], '0')

    def test_stopped_vm_no_writes(self):
        self.domain = '-'
        self.a.tick(self.cfg)
        self.assertEqual(self.writes, [])

    def test_validation(self):
        self.assertEqual(validate({})['free_affinity'], [])
        with self.assertRaises(ValueError):
            validate(dict(free_affinity=['--bad']))
        self.assertEqual(cpus('0-2,2,4'), '0,1,2,4')


if __name__ == '__main__':
    unittest.main()
