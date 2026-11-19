/* Policy v4 纯函数层：工厂、深拷贝、行为 canonical 与软提示（Task 8）
   ------------------------------------------------------------------
   只做数据：不读 DOM、不发请求、不碰 localStorage。
   · createPolicy —— 前端唯一的新建文档来源（与 spec §4.1 同构）；
   · behaviorCanonical —— 递归稳定排序对象键、保留数组顺序并剔除 name，
     只用于前端 dirty 比较；正式 policy_revision 永远由后端计算，不要求
     Python/JS 跨语言字节对齐；
   · optionCountHint —— choice 选项超过 8 个时的非阻塞评测建议（≤8 为空）。
   ------------------------------------------------------------------ */
import { tf } from '../i18n.js';

export function clone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

export function createPolicy(modelId = 'qwen3-8b') {
  return {
    kind: 'sr-routing-policy',
    version: 4,
    name: '新策略',
    timezone: 'Asia/Shanghai',
    sticky: { enabled: false, ttl_seconds: 1800 },
    questions: {
      route: {
        type: 'choice',
        instructions: '请选择最适合处理这条请求的模型。',
        options: [
          { key: 'fast', label: '轻量模型', capability: '适合简单请求。' },
          { key: 'strong', label: '增强模型', capability: '适合复杂请求。' }
        ]
      }
    },
    targets: {
      fast: { models: [{ model: modelId }] },
      strong: { models: [{ model: modelId }] },
      safe: { models: [{ model: modelId }] }
    },
    rules: [
      { name: '轻量请求', when: { answer: 'route.selected', eq: 'fast' }, target: 'fast' },
      { name: '增强请求', when: { answer: 'route.selected', eq: 'strong' }, target: 'strong' }
    ],
    fallback: 'safe'
  };
}

export function behaviorCanonical(policy) {
  const copy = clone(policy);
  delete copy.name;
  const normalize = value => {
    if (Array.isArray(value)) return value.map(normalize);
    if (value && typeof value === 'object') {
      return Object.fromEntries(Object.keys(value).sort().map(key => [key, normalize(value[key])]));
    }
    return value;
  };
  return JSON.stringify(normalize(copy));
}

/* 非阻塞评测建议：选项越多，单次 JEV 的提示越长、判断越不稳定。
   只返回文案，不禁用保存；最终校验仍以后端 422 为准。 */
export function optionCountHint(question) {
  const options = question && Array.isArray(question.options) ? question.options : [];
  if (options.length <= 8) return '';
  return tf('pv.optionHint', { n: options.length });
}
