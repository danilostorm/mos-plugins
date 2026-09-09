// MOS federation contract: reuse the host Vue runtime; no CDN or second Vue copy.
import { createPlugin } from './component.js';
export async function get(name) {
  if (name !== './Plugin') throw new Error('Unknown module');
  const shared = globalThis.__federation_shared__?.default?.vue;
  if (!shared) throw new Error('MOS Vue runtime unavailable');
  const entry = Object.values(shared)[0];
  const factory = await entry.get();
  const Vue = factory();
  const component = createPlugin(Vue);
  return () => ({ default: component });
}
export function init() {}
