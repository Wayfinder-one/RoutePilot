/* RoutePilot console · 决策服务目录页数据层（多 JEV 服务 CRUD / 逐条连接测试）
   ------------------------------------------------------------------
   本模块是页面唯一入口：静态 import 视图层（jev-view.js），把五个真实异步动作
   `initJevView({ listServices, createService, updateService, deleteService,
   testService })` 交给它，然后启动视图。依赖单向（jev.js → jev-view.js），
   视图不 import 本模块，因此没有循环依赖，也没有合成 click / MutationObserver
   保存编排。

   服务端点契约（多 JEV 目录，spec「多 JEV 目录 / 管理 API 与页面」）：
   - `GET    /admin/jev`                   → `{services: [publicService…]}`
   - `POST   /admin/jev`                   → `{service}`
   - `PUT    /admin/jev/{id}`              → `{service, synced_trees}`
   - `DELETE /admin/jev/{id}`（可选 body）  → `{deleted, replacement_jev_id, synced_trees}`
   - `POST   /admin/jev/{id}/test`         → 连接测试结果
   被已存画布引用的服务在更新/删除时会先返回 409 `impact_confirmation_required`；
   数据层只负责把调用方给的确认载荷原样透传（`impact_confirmation`，删除另加
   `replacement_jev_id`），绝不自行重试；两阶段确认由视图经共享 impact.js 编排。

   `nonSecretServiceBody(draft)` 是写请求的完整白名单：api_key 只在用户输入了新
   密钥或勾选“清除”时才随写请求发送，响应/列表状态里永不保存密钥。
   ------------------------------------------------------------------ */
import { api } from './api.js';
import { initJevView } from './jev-view.js';
import './nav.js';

/* 写请求的非密钥字段白名单；新增字段必须同时改这里和服务端 DTO。 */
export function nonSecretServiceBody(draft) {
  return {
    id: draft.id,
    base_url: draft.baseUrl,
    model: draft.model,
    key_env: draft.keyEnv || '',
    timeout_seconds: Number(draft.timeoutSeconds || 10),
    instructions: draft.instructions,
    state_template: draft.stateTemplate,
    input_tokens: Number(draft.inputTokens),
    context_tokens: Number(draft.contextTokens)
};
}

function writeBody(draft) {
  const body = nonSecretServiceBody(draft);
  /* 只在用户显式提供新密钥 / 显式清除时发送 api_key；留空 = 保留已存密钥。 */
  if (draft.clearKey) body.api_key = '';
  else if (draft.apiKey) body.api_key = String(draft.apiKey);
  return body;
}

export async function listServices() {
  const body = await api.get('/admin/jev');
  return Array.isArray(body.services) ? body.services : [];
}

export async function createService(draft) {
  const saved = await api.post('/admin/jev', writeBody(draft));
  return saved.service;
}

export async function updateService(id, draft, impactConfirmation) {
  const body = writeBody(draft);
  /* 确认载荷只在调用方显式提供时透传；确认内容由共享 impact.js 从 409 detail
     构造，数据层不猜哈希。 */
  if (impactConfirmation) body.impact_confirmation = impactConfirmation;
  /* 返回**完整响应** `{service, synced_trees}`：synced_trees 是“已打开画布需
     重新加载”提示的唯一来源，只回 service 会让 JEV 更新后的同步反馈丢失。 */
  return api.put('/admin/jev/' + encodeURIComponent(id), body);
}

export async function deleteService(id, confirmation) {
  /* confirmation = { replacement_jev_id?, impact_confirmation? }（共享确认重试时
     由视图传入）；未被引用的服务不带 body 直接删除。 */
  const body = {};
  if (confirmation && typeof confirmation === 'object') Object.assign(body, confirmation);
  return api.req('DELETE', '/admin/jev/' + encodeURIComponent(id),
    Object.keys(body).length ? body : undefined);
}

export async function testService(id, text) {
  /* 决策服务单次测试可能跑满可配置的 120s 超时；全局 15s 管理面超时不能中途掐断。 */
  return api.post('/admin/jev/' + encodeURIComponent(id) + '/test',
    { text: String(text == null ? '' : text) }, { timeoutMs: 130000 });
}

initJevView({ listServices, createService, updateService, deleteService, testService });
