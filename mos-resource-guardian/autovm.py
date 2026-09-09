"""Import saved MOS/libvirt limits without rewriting the VM editor or XML."""
import copy
import math
import re
import time
import xml.etree.ElementTree as ET
from control import command


def definition(xml):
    root = ET.fromstring(xml)
    uuid = root.findtext('uuid', '')
    if root.get('type') != 'kvm' or not re.fullmatch(r'[0-9a-fA-F-]{36}', uuid):
        raise ValueError('Only persistent KVM domains with UUID are supported')
    cpu = root.find('vcpu')
    memory = root.find('memory')
    if cpu is None or memory is None:
        raise ValueError('Missing saved CPU/memory limits')
    cpus = int(cpu.get('current', cpu.text or '0'))
    units = {'b': 1/1048576, 'bytes': 1/1048576, 'kib': 1/1024,
             'kb': 1000/1048576, 'mib': 1, 'mb': 1000000/1048576,
             'gib': 1024, 'gb': 1000000000/1048576}
    unit = memory.get('unit', 'KiB').lower()
    if unit not in units:
        raise ValueError('Unsupported memory unit')
    ceiling = math.floor(int(memory.text) * units[unit])
    if not 1 <= cpus <= 4096 or not 256 <= ceiling <= 16777216:
        raise ValueError('Invalid saved limits')
    balloon = root.find('./devices/memballoon')
    safe = (balloon is not None and balloon.get('model') == 'virtio' and
            root.find('./memoryBacking/hugepages') is None and
            root.find('./memoryBacking/locked') is None)
    pinned = root.find('./cputune/vcpupin') is not None or cpu.get('cpuset') is not None
    target = dict(kind='vm', id=uuid, priority=50, cpu_method='quota', cpu_min=1,
                  cpu_max=cpus, manage_memory=safe, memory_autoscale=True,
                  memory_min_mib=min(ceiling, max(1024, min(4096, ceiling//2))),
                  memory_max_mib=ceiling, memory_headroom_mib=512)
    return target, dict(uuid=uuid, name=root.findtext('name', uuid), cpu_max=cpus,
                        memory_max_mib=ceiling, memory_min_mib=target['memory_min_mib'],
                        pinned=pinned, memory_supported=safe,
                        note='Pinning existente preservado; desmarque no editor para distribuição livre.' if pinned else
                             'Balloon indisponível ou memória especial; apenas CPU.' if not safe else 'Limites sincronizados do MOS.')


class AutoVM:
    def __init__(self, runner=command):
        self.run = runner
        self.last = -float('inf')
        self.cached = []
        self.catalog = []
        self.polling = {}

    def resolve(self, cfg):
        if not cfg['auto_vms']:
            self.cached, self.catalog = [], []
            self.last = -float('inf')
            return cfg
        if time.monotonic()-self.last >= 10:
            targets, catalog = [], []
            try:
                uuids = self.run(['virsh', '--connect', 'qemu:///system', 'list', '--all', '--persistent', '--uuid']).splitlines()
                if len(uuids) > 32:
                    raise ValueError('Mais de 32 VMs: use configuração manual')
                for uuid in uuids:
                    if not uuid.strip():
                        continue
                    try:
                        if not re.fullmatch(r'[0-9a-fA-F-]{36}', uuid):
                            raise ValueError('Invalid domain UUID')
                        xml = self.run(['virsh', '--connect', 'qemu:///system', 'dumpxml', uuid, '--inactive'])
                        target, item = definition(xml)
                        targets.append(target)
                        catalog.append(item)
                    except Exception as exc:
                        catalog.append(dict(uuid=uuid, error=str(exc)[:200]))
            except Exception as exc:
                catalog = [dict(error=str(exc)[:200])]
                targets = []  # Never reuse stale limits after failed discovery.
            self.cached, self.catalog, self.last = targets, catalog, time.monotonic()
        resolved = copy.deepcopy(cfg)
        manual = {t['id'] for t in cfg['targets'] if t['kind'] == 'vm'}
        names = {x.get('uuid'): x.get('name') for x in self.catalog}
        excluded = set(cfg['auto_vm_exclude'])
        for t in self.cached:
            if t['id'] not in manual | excluded and names.get(t['id']) not in manual:
                resolved['targets'].append(copy.deepcopy(t))
        if len(resolved['targets']) > 32:
            resolved['targets'] = copy.deepcopy(cfg['targets'])
            self.catalog = [dict(error='Limite de 32 alvos excedido; importação automática suspensa')]
        return resolved

    def enable_stats(self, cfg):
        # Only an explicit automatic mode may change live balloon polling.
        if not cfg['auto_vms'] or cfg['mode'] != 'automatic':
            return
        auto_ids = {t['id'] for t in self.cached}
        for t in cfg['targets']:
            if t['kind'] != 'vm' or t['id'] not in auto_ids or not t['manage_memory']:
                continue
            try:
                domain_id = self.run(['virsh', '--connect', 'qemu:///system', 'domid', t['id']])
                if not domain_id.isdigit():
                    continue
                key = (t['id'], domain_id)
                if time.monotonic()-self.polling.get(key, -float('inf')) < 60:
                    continue
                self.polling[key] = time.monotonic()
                self.run(['virsh', '--connect', 'qemu:///system', 'dommemstat', t['id'], '--period', '5', '--live'])
            except Exception:
                pass  # Adapter still requires fresh guest stats before ballooning.
