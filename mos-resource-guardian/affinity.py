"""Live vCPU affinity with durable originals and compare-before-restore."""
import json
import re
from pathlib import Path
from control import command


def cpus(value):
    result = set()
    for part in value.strip().split(','):
        if not re.fullmatch(r'\d+(?:-\d+)?', part):
            raise ValueError('Invalid CPU list')
        bounds = list(map(int, part.split('-')))
        lo, hi = bounds[0], bounds[-1]
        if not 0 <= lo <= hi < 65536:
            raise ValueError('Invalid CPU range')
        result.update(range(lo, hi+1))
    return ','.join(map(str, sorted(result)))


class Affinity:
    def __init__(self, db, runner=command, boot=None, online=None):
        self.db, self.run = db, runner
        self.boot = boot or Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.online = online or (lambda: Path('/sys/devices/system/cpu/online').read_text())
        db.execute('CREATE TABLE IF NOT EXISTS affinity (uuid TEXT PRIMARY KEY, payload TEXT)')
        db.commit()
        self.status = []

    def virsh(self, uuid, op, *args):
        return self.run(['virsh', '--connect', 'qemu:///system', op, uuid, *args]).strip()

    def snapshot(self, uuid):
        identity = self.boot + ':' + self.virsh(uuid, 'domid')
        if not identity.split(':')[-1].isdigit():
            raise ValueError('VM parada; aguardando inicialização')
        pins = {}
        for line in self.virsh(uuid, 'vcpupin', '--live').splitlines():
            match = re.fullmatch(r'\s*(\d+)\s+([\d,\-]+)\s*', line)
            if match:
                pins[match[1]] = cpus(match[2])
        if not pins:
            raise ValueError('Não foi possível consultar afinidade')
        if self.boot + ':' + self.virsh(uuid, 'domid') != identity:
            raise ValueError('VM reiniciou durante consulta')
        return identity, pins

    def save(self, uuid, entry):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO affinity VALUES (?,?)', (uuid, json.dumps(entry)))

    def tick(self, cfg, restore=False):
        selected = set(cfg.get('free_affinity', []))
        eligible = {t['id'] for t in cfg['targets'] if t['kind'] == 'vm'}
        entries = {u: json.loads(p) for u, p in self.db.execute('SELECT * FROM affinity')}
        desired = (selected - set(cfg['auto_vm_exclude'])) & (eligible | set(entries)) if cfg['auto_vms'] else set()
        self.status = []
        for uuid in sorted(desired | set(entries)):
            try:
                identity, pins = self.snapshot(uuid)
                entry = entries.get(uuid)
                if entry and entry['identity'] != identity:
                    with self.db:
                        self.db.execute('DELETE FROM affinity WHERE uuid=?', (uuid,))
                    entry = None  # A new runtime starts with MOS persistent settings.
                undo = restore or uuid not in desired or bool(entry and entry.get('restoring'))
                if not undo and cfg['mode'] != 'automatic':
                    self.status.append(dict(uuid=uuid, message='Pausado / observação; afinidade mantida'))
                    continue
                if not entry:
                    if undo:
                        continue
                    entry = dict(identity=identity, original=pins, applied={}, goal=cpus(self.online()))
                    self.save(uuid, entry)
                if undo:
                    entry['restoring'] = True
                    self.save(uuid, entry)
                for vcpu, original in entry['original'].items():
                    now_identity, current = self.snapshot(uuid)
                    if now_identity != identity:
                        raise ValueError('VM reiniciou; aguardando novo ciclo')
                    value = current.get(vcpu)
                    expected = entry['applied'].get(vcpu, original)
                    if value not in (original, expected):
                        raise ValueError('Afinidade alterada externamente; restauração manual necessária')
                    goal = original if undo else entry['goal']
                    if value == goal:
                        continue
                    if not undo and vcpu in entry['applied'] and value != expected:
                        raise ValueError('Afinidade alterada externamente; ajuste suspenso')
                    # Commit intent before each write, including partially completed batches.
                    if not undo:
                        entry['applied'][vcpu] = goal
                        self.save(uuid, entry)
                    self.virsh(uuid, 'vcpupin', vcpu, goal, '--live')
                    check_identity, check = self.snapshot(uuid)
                    if check_identity != identity or check.get(vcpu) != goal:
                        raise ValueError('Afinidade não confirmada')
                if undo:
                    with self.db:
                        self.db.execute('DELETE FROM affinity WHERE uuid=?', (uuid,))
                self.status.append(dict(uuid=uuid, message='Afinidade original restaurada' if undo else 'CPU automática: afinidade das vCPUs liberada'))
            except Exception as exc:
                self.status.append(dict(uuid=uuid, message=str(exc)[:250]))
