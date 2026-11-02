import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';
const require = createRequire(import.meta.url);
const MOCK = require('../docs/od-design/routing-canvas-2026-11-02/mock.js');
const nodes = MOCK.nodes.map(node => {
  if (node.type !== 'target') return structuredClone(node);
  const copy = structuredClone(node);
  copy.models = [{ model: copy.model, reasoning: copy.reasoning ?? null }];
  delete copy.model;
  delete copy.reasoning;
  delete copy.backends;
  return copy;
});
const root = { ...structuredClone(MOCK.graph), nodes };
const now = {
  s1: '2026-11-02T12:00:00+08:00', s2: '2026-11-02T23:30:00+08:00',
  s3: '2026-11-02T09:00:00+08:00', s4: '2026-11-02T12:00:00+08:00',
  s5: '2026-11-02T23:30:00+08:00',
};
const scenarios = MOCK.scenarios.map(s => ({ ...structuredClone(s), now: now[s.id] }));
const periods = [
  { name: 'peak', days: [0,1,2,3,4,5,6], start: '08:00', end: '11:00' },
  { name: 'normal', days: [0,1,2,3,4,5,6], start: '11:00', end: '18:00' },
  { name: 'offpeak', days: [0,1,2,3,4,5,6], start: '18:00', end: '08:00' },
];
writeFileSync(new URL('../tests/fixtures/graph_power.json', import.meta.url), JSON.stringify(root, null, 2) + '\n');
writeFileSync(new URL('../tests/fixtures/graph_scenarios.json', import.meta.url), JSON.stringify({ periods, scenarios }, null, 2) + '\n');
console.log(`ported ${nodes.length} nodes/${MOCK.edges.length} edges/${scenarios.length} scenarios`);
