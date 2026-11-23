/* RoutePilot 控制台 · 共享影响确认纯逻辑直测（node --test）
   ------------------------------------------------------------------
   只测 `router/console/impact.js` 的纯函数（不启动浏览器 / 服务）：
   - 409 两阶段协议的错误识别（impact_confirmation_required / graph_conflict /
     provider_referenced）；
   - `confirmationFrom` 必须只携带受影响画布的 id + **精确**
     document_sha256（不夹带 name/enabled/usage 等服务端可能拒绝的字段，
     也不重新计算哈希）；
   - 替代项需求判定（模型删除只有 target 引用才要替代；JEV 删除只要被引用
     就必须替代；更新永不需要）。

   页面级行为（影响清单只展示一次、取消不发请求、确认请求体、陈旧哈希提示）
   属于真实请求/响应断言，注册在 scripts/console_qa/models.mjs 与
   scripts/console_qa/jev.mjs（与 QA 基座同一套启动/teardown）。

   运行：`node --test scripts/console_impact_test.mjs`
   ------------------------------------------------------------------ */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  confirmationFrom,
  isGraphConflict,
  isImpactError,
  isProviderReferenced,
  replacementLabel,
  requiresReplacement,
} from '../router/console/impact.js';

const HASH_A = 'a'.repeat(64);
const HASH_B = 'b'.repeat(64);

function impactError(detail) {
  return { status: 409, detail };
}

test('isImpactError only matches 409 impact_confirmation_required', () => {
  assert.equal(isImpactError(impactError({
    code: 'impact_confirmation_required',
    resource: { kind: 'model', id: 'm1' },
    affected_trees: [{ id: 'main', document_sha256: HASH_A }],
    replacements: [],
  })), true);

  /* 非 409 / 其他 code / 缺 detail / 非对象：全部 false。 */
  assert.equal(isImpactError({ status: 400, detail: { code: 'impact_confirmation_required' } }), false);
  assert.equal(isImpactError({ status: 409, detail: { code: 'graph_conflict' } }), false);
  assert.equal(isImpactError({ status: 409, detail: { code: 'provider_referenced' } }), false);
  assert.equal(isImpactError({ status: 409, detail: 'impact_confirmation_required' }), false);
  assert.equal(isImpactError({ status: 409 }), false);
  assert.equal(isImpactError(new Error('boom')), false);
  assert.equal(isImpactError(null), false);
  assert.equal(isImpactError(undefined), false);
});

test('confirmationFrom builds exact tree hashes from the 409 detail', () => {
  const detail = {
    code: 'impact_confirmation_required',
    resource: { kind: 'model', id: 'qwen3-32b' },
    affected_trees: [
      { id: 'main', name: '主画布', enabled: true, document_sha256: HASH_A,
        usage: { targets: ['power-target'], runtime_catalog: true } },
      { id: 'backup', name: '备份画布', enabled: false, document_sha256: HASH_B,
        usage: { targets: [], runtime_catalog: true } },
    ],
    replacements: [{ id: 'qwen3-8b', upstream: 'qwen3-8b', provider: 'local' }],
  };
  assert.deepEqual(confirmationFrom(detail), {
    trees: [
      { id: 'main', document_sha256: HASH_A },
      { id: 'backup', document_sha256: HASH_B },
    ],
  });
  /* 输出严格只有 id/hash：不接受 name/enabled/usage 夹带。 */
  for (const tree of confirmationFrom(detail).trees) {
    assert.deepEqual(Object.keys(tree).sort(), ['document_sha256', 'id']);
  }
});

test('confirmationFrom tolerates missing or malformed affected_trees', () => {
  assert.deepEqual(confirmationFrom({}), { trees: [] });
  assert.deepEqual(confirmationFrom(null), { trees: [] });
  assert.deepEqual(confirmationFrom({ affected_trees: 'not-a-list' }), { trees: [] });
  /* 单棵树缺 hash 时原样保留 undefined（不做假数据填充，交给服务端 409）。 */
  assert.deepEqual(confirmationFrom({ affected_trees: [{ id: 'main' }] }),
    { trees: [{ id: 'main', document_sha256: undefined }] });
});

test('requiresReplacement follows the backend delete semantics', () => {
  const targetUsage = {
    resource: { kind: 'model', id: 'qwen3-32b' },
    affected_trees: [{ id: 'main', document_sha256: HASH_A,
      usage: { targets: ['power-target'], runtime_catalog: true } }],
  };
  const catalogOnly = {
    resource: { kind: 'model', id: 'qwen3-32b' },
    affected_trees: [{ id: 'main', document_sha256: HASH_A,
      usage: { targets: [], runtime_catalog: true } }],
  };
  const jevDetail = {
    resource: { kind: 'jev', id: 'default' },
    affected_trees: [{ id: 'main', document_sha256: HASH_A, usage: { jev: 'default' } }],
  };
  const providerDetail = {
    resource: { kind: 'provider', name: 'local' },
    affected_trees: [{ id: 'main', document_sha256: HASH_A,
      usage: { runtime_catalog: true, models: { use: ['qwen3-8b'] } } }],
  };

  assert.equal(requiresReplacement(targetUsage, 'delete'), true,
    'model delete with a target reference needs replacement_model_id');
  assert.equal(requiresReplacement(catalogOnly, 'delete'), false,
    'model delete with only runtime-catalog usage does not need a replacement');
  assert.equal(requiresReplacement(jevDetail, 'delete'), true,
    'JEV delete while referenced needs a replacement service');
  assert.equal(requiresReplacement(providerDetail, 'delete'), false,
    'provider delete never takes a replacement (it is non-bypassable)');
  /* 更新不需要替代项，即使画布有 target 引用。 */
  assert.equal(requiresReplacement(targetUsage, 'update'), false);
  assert.equal(requiresReplacement(jevDetail, 'update'), false);
  /* 缺省 / 空 detail 永远不要求替代项。 */
  assert.equal(requiresReplacement(null, 'delete'), false);
  assert.equal(requiresReplacement({}, 'delete'), false);
  assert.equal(requiresReplacement({ resource: { kind: 'jev' }, affected_trees: [] }, 'delete'), false);
});

test('sibling 409 codes are detected without being impact confirmations', () => {
  const conflict = { status: 409, detail: { code: 'graph_conflict', actual_document_sha256: HASH_B } };
  const blocked = { status: 409, detail: { code: 'provider_referenced', models: ['qwen3-8b'] } };
  assert.equal(isGraphConflict(conflict), true);
  assert.equal(isGraphConflict(blocked), false);
  assert.equal(isGraphConflict({ status: 409, detail: { code: 'impact_confirmation_required' } }), false);
  assert.equal(isProviderReferenced(blocked), true);
  assert.equal(isProviderReferenced(conflict), false);
  assert.equal(isGraphConflict(null), false);
  assert.equal(isProviderReferenced(undefined), false);
});

test('replacementLabel shows non-secret catalog facts', () => {
  assert.equal(replacementLabel({ id: 'qwen3-8b', upstream: 'qwen3-8b', provider: 'local' }),
    'qwen3-8b · qwen3-8b');
  assert.equal(replacementLabel({ id: 'default', base_url: 'http://127.0.0.1:8100', model: 'd1-2b' }),
    'default · d1-2b');
  assert.equal(replacementLabel({ id: 'only-id' }), 'only-id');
  assert.equal(replacementLabel(null), '');
  assert.equal(replacementLabel({}), '');
  /* 替代项投影里不应出现密钥材料；即便被塞入也不会渲染出来。 */
  assert.equal(replacementLabel({ id: 'x', api_key: 'sk-leak' }), 'x');
});
