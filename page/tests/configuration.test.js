import test from 'node:test';
import assert from 'node:assert/strict';
import { createPlugin, encodeConfig, confirms } from '../component.js';

const forbidden = /[;&|`$(){}\[\]<>\n\r]/;
test('configuration transport meets MOS query argument rules and preserves Unicode', () => {
  const data = { auto_vms: true, targets: [], data_directory: '/mnt/disco-ação/guardian' };
  assert.equal(forbidden.test(JSON.stringify(data)), true);
  const arg = encodeConfig(data);
  assert.equal(forbidden.test(arg), false);
  assert.deepEqual(JSON.parse(Buffer.from(arg.slice(4), 'base64').toString('utf8')), data);
  assert.equal(confirms({auto_vms: false}, {auto_vms: true}), false);
  assert.equal(confirms({auto_vms: true, free_affinity: []}, {auto_vms: true}), true);
});

test('panel saves, confirms and retains errors and draft when persistence fails', async () => {
  const initial = {auto_vms: false, auto_vm_exclude: [], free_affinity: [], targets: [], mode: 'observe', monitor: {}, profile: 'automatic'};
  let persisted = structuredClone(initial), rejectSave = false, mounted, unmounted;
  const priorFetch = globalThis.fetch, priorStorage = globalThis.localStorage;
  globalThis.localStorage = {getItem: () => 'test-token'};
  globalThis.fetch = async (_, options) => {
    const {args} = JSON.parse(options.body);
    assert.ok(args.every(x => !forbidden.test(x)));
    let output;
    switch (args[0]) {
      case 'config': output = structuredClone(persisted); break;
      case 'configure':
        if (!rejectSave) persisted = JSON.parse(Buffer.from(args[1].slice(4), 'base64').toString());
        output = {ok: true}; break;
      case 'status': output = {state: 'normal', auto_vms: []}; break;
      case 'history': output = []; break;
      case 'discover': output = {vms: [{uuid: 'test-vm', name: 'DANILO', cpu_max: 16, memory_max_mib: 16384}]}; break;
      default: throw Error(args[0]);
    }
    return {ok: true, json: async () => ({success: true, output})};
  };
  const h = (tag, attrs, children) => {
    if (children === undefined && (Array.isArray(attrs) || typeof attrs === 'string')) return {tag, attrs: {}, children: attrs};
    return {tag, attrs: attrs || {}, children};
  };
  const render = createPlugin({h, ref: value => ({value}), onMounted: f => {mounted = f;}, onUnmounted: f => {unmounted = f;}}).setup();
  function nodes(node, result=[]) { if (node && typeof node === 'object') { result.push(node); for (const child of [node.children].flat(Infinity)) nodes(child, result); } return result; }
  const button = label => nodes(render()).find(x => x.tag === 'button' && x.children === label);
  const checkbox = () => nodes(render()).find(x => x.tag === 'input' && x.attrs.type === 'checkbox');
  try {
    await mounted();
    assert.ok(JSON.stringify(render()).includes('DANILO'), 'discovery before opt-in');
    checkbox().attrs.onChange({target: {checked: true}});
    await button('Salvar configuração').attrs.onClick();
    assert.equal(persisted.auto_vms, true);
    assert.equal(checkbox().attrs.checked, true);
    rejectSave = true;
    checkbox().attrs.onChange({target: {checked: false}});
    await button('Salvar configuração').attrs.onClick();
    assert.equal(checkbox().attrs.checked, false, 'unsaved draft preserved');
    await button('Atualizar').attrs.onClick();
    assert.ok(JSON.stringify(render()).includes('não confirmou'), 'polling must not erase mutation error');
  } finally { unmounted(); globalThis.fetch = priorFetch; globalThis.localStorage = priorStorage; }
});
