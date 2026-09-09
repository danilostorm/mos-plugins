import assert from 'node:assert/strict';
import test from 'node:test';
import { get } from '../remoteEntry.js';

test('MOS shared Vue runtime loads and renders the native plugin', async () => {
  const Vue = {
    h: (tag, attrs, children) => ({ tag, attrs, children }),
    ref: value => ({ value }), onMounted: () => {}, onUnmounted: () => {}
  };
  globalThis.__federation_shared__ = { default: { vue: { '3': { get: () => () => Vue } } } };
  const factory = await get('./Plugin');
  const component = factory().default;
  assert.equal(component.name, 'ResourceGuardian');
  const vnode = component.setup()();
  assert.equal(vnode.tag, 'section');
  assert.equal(vnode.attrs.class, 'resource-guardian');
  await assert.rejects(get('./Untrusted'));
});

test('missing MOS runtime fails clearly instead of downloading code', async () => {
  delete globalThis.__federation_shared__;
  await assert.rejects(get('./Plugin'), /runtime unavailable/);
});
