/* Shared routing-trace renderer (console, 2026-10-26).

   ONE renderer for every routing trace in the console: the pipeline try drawer
   and the calculation-log detail both call
   `SRTraceView.renderRoutingTrace(container, result, {tree})`, so the same
   backend event renders the same text on both pages (task-14 smoke asserts
   this).

   Inputs are the backend DTOs:
   - `/admin/try` result: `outcome {type,kind,model,reasoning,node_id,score}` /
     `{type:"miss",reason}`, `trace` events, tree_id/tree_revision/
     snapshot_revision, timing fields;
   - a memory-ring log row (no `outcome`): decision/model/terminal_node_id/
     score/reasoning plus the same trace and revisions. `outcomeOf()` derives
     the summary for both shapes.
   - `{tree}` is the tree list row (id/name) when known; the renderer never
     fetches anything.

   Trace events come from `router/pipeline/evaluator.py`:
     condition           {signal,op,value,m,conf,conf_min,pass,reason}
     hit / miss          terminal {kind,model,reasoning,score,min_score,lines}
     nl_choice           {candidates,probabilities,ranking,selected_key,
                          min_prob,min_margin,p_top,margin,reason,duration_ms,
                          nl_call,input,model_info,error}
     selected_child_miss {reason,selected_key,selected_node_id}
     otherwise_hit / otherwise_miss {reason}
   Unknown event names / reason codes render their raw code (never dropped).
   NL candidates are ALWAYS listed in the configured candidate order; the
   sorted `ranking` is only used for a "top" marker, never for reordering.

   Browser-only; loaded after api.js/shared.js (uses esc/t/sigName/valLabel
   when present, with built-in fallbacks so it is harness-testable). */
"use strict";

const SRTraceView = (function () {
  /* ── labels (zh fallbacks; t() wins when the dictionary has the key) ── */
  const FALLBACK = {
    "trace.outcome.route": "命中路由",
    "trace.outcome.block": "命中拦截",
    "trace.outcome.miss": "未命中",
    "trace.reasoning": "推理",
    "trace.score": "得分",
    "trace.scoreOf": "得分 {score} ≥ {min}",
    "trace.tree": "路由树",
    "trace.treeRevision": "树 revision",
    "trace.snapshotRevision": "快照 revision",
    "trace.timing": "信号 {signal}ms · 决策 {decision}ms · NL 调用 {calls} 次",
    "trace.events": "判定轨迹",
    "trace.noTrace": "没有轨迹（记录里没有事件）",
    "trace.invalidEvent": "无效事件",
    "trace.event.condition": "条件判断",
    "trace.event.hit": "命中终止节点",
    "trace.event.miss": "未命中终止节点",
    "trace.event.nl_choice": "自然语言选择",
    "trace.event.selected_child_miss": "选中子树未命中",
    "trace.event.otherwise_hit": "兜底出边命中",
    "trace.event.otherwise_miss": "兜底出边未命中",
    "trace.event.entered": "进入容器节点",
    "trace.event.invalid": "无效事件",
    "trace.reason.condition_false": "条件不匹配",
    "trace.reason.below_confidence": "置信度低于门槛",
    "trace.reason.below_min_score": "达成率低于门槛",
    "trace.reason.nl_low_probability": "最高候选概率低于门槛",
    "trace.reason.nl_ambiguous": "前两名概率差低于门槛",
    "trace.reason.nl_error": "自然语言调用失败",
    "trace.reason.selected_child_miss": "选中的子树未命中",
    "trace.reason.no_hit": "没有任何子节点命中",
    "trace.reason.routing_invariant_violation": "健康树不应未命中（不变量违反）",
    "trace.reason.signal_preparation_failed": "信号准备失败",
    "trace.condition.pass": "通过",
    "trace.condition.fail": "未通过",
    "trace.condition.deterministic": "确定性",
    "trace.condition.conf": "置信",
    "trace.hit.block": "拦截",
    "trace.weights": "加权条件（达成率）",
    "trace.lines.signal": "信号",
    "trace.lines.contrib": "贡献",
    "trace.nl.title": "候选概率（按配置顺序）",
    "trace.nl.top": "最高",
    "trace.nl.selected": "选中",
    "trace.nl.pTop": "最高概率",
    "trace.nl.margin": "概率差",
    "trace.nl.minProb": "门槛 p_top ≥ {value}",
    "trace.nl.minMargin": "门槛 margin ≥ {value}",
    "trace.nl.duration": "耗时 {value}ms",
    "trace.nl.call": "NL 调用",
    "trace.nl.evidence": "NL 调用证据",
    "trace.nl.error": "调用错误",
    "trace.selected": "选中",
    "trace.selectedNode": "选中子树",
    "trace.superseded": "已被直接命中取代",
    "trace.otherwiseHit": "兜底出边返回了结果",
    "trace.otherwiseMiss": "兜底出边也未命中",
  };
  const EFFORT_FALLBACK = {
    off: "透传", minimal: "最低", low: "低", medium: "中", high: "高",
    xhigh: "最高", max: "最高",
  };
  function tr(key, vars) {
    let text = FALLBACK[key] != null ? FALLBACK[key] : key;
    try {
      if (typeof t === "function") {
        const hit = t(key);
        if (hit && hit !== key) text = hit;
      }
    } catch (err) { /* i18n not ready */ }
    if (vars) {
      Object.keys(vars).forEach(k => { text = text.split("{" + k + "}").join(String(vars[k])); });
    }
    return text;
  }
  function escHTML(value) {
    if (typeof esc === "function") return esc(value);
    return String(value == null ? "" : value)
      .replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  }
  function sigLabel(key) {
    try { if (typeof sigName === "function") return sigName(key); } catch (err) { /* noop */ }
    return String(key == null ? "" : key);
  }
  function valueLabel(key, value) {
    try {
      if (typeof valLabel === "function") {
        const label = valLabel(key, value);
        if (label) return label;
      }
    } catch (err) { /* noop */ }
    return String(value == null ? "" : value);
  }
  function isRuleSignal(key) {
    try { if (typeof isRuleSig === "function") return isRuleSig(key); } catch (err) { /* noop */ }
    return false;
  }
  function effortLabel(effort) {
    if (effort == null || effort === "") return "";
    try {
      if (typeof t === "function") {
        const hit = t("reason." + effort);
        if (hit && hit !== "reason." + effort) return hit;
      }
    } catch (err) { /* noop */ }
    return EFFORT_FALLBACK[String(effort)] || String(effort);
  }
  function reasonEffort(effort) { return effortLabel(effort); }
  const KIND_FALLBACK = {
    branch: "普通分支", nl_choice: "自然语言选择", route: "路由", block: "拦截",
  };
  function entryKindLabel(kind) {
    if (kind == null || kind === "") return "";
    const key = "tree." + String(kind);
    const label = tr(key);
    if (label !== key) return label;
    return KIND_FALLBACK[String(kind)] || String(kind);
  }

  /* ── human text for event names / reason codes (raw fallback) ──────── */
  function eventLabel(name) {
    const key = "trace.event." + String(name);
    const label = tr(key);
    if (label !== key) return label;
    return name == null ? tr("trace.invalidEvent") : String(name);
  }
  function reasonLabel(code) {
    if (code == null || code === "") return "";
    const key = "trace.reason." + String(code);
    const label = tr(key);
    return label !== key ? label : String(code);
  }

  /* ── value formatting ───────────────────────────────────────────────── */
  function num(value) {
    if (value == null || value === "") return "—";
    const number = Number(value);
    return Number.isFinite(number) ? number.toFixed(2) : String(value);
  }
  function pctWidth(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return 0;
    return Math.round(100 * Math.max(0, Math.min(1, number)));
  }
  function shortRev(value, length) {
    if (value == null || value === "") return "—";
    const text = String(value);
    return text.length > (length || 12) ? text.slice(0, length || 12) + "…" : text;
  }
  function barHTML(value, floor, cut) {
    const width = pctWidth(value);
    const tick = floor == null ? ""
      : `<u class="rtt-tick" style="left:${pctWidth(floor)}%"></u>`;
    return `<span class="rtt-bar${cut ? " cut" : ""}"><i style="width:${width}%"></i>${tick}</span>`;
  }

  /* ── outcome normalisation (try DTO + log row) ─────────────────────── */
  function outcomeOf(result) {
    const dto = result && typeof result === "object" ? result : {};
    const outcome = dto.outcome && typeof dto.outcome === "object" ? dto.outcome
      : null;
    if (outcome) {
      const type = String(outcome.type || "").toLowerCase();
      const kind = type === "miss" || (!outcome.kind && !outcome.model && outcome.reason)
        ? "miss"
        : String(outcome.kind || (outcome.model ? "route" : "miss"));
      return {
        kind: kind === "block" || kind === "route" ? kind : "miss",
        model: outcome.model == null ? null : String(outcome.model),
        reasoning: outcome.reasoning == null ? null : String(outcome.reasoning),
        node_id: outcome.node_id == null ? null : String(outcome.node_id),
        score: outcome.score == null ? null : Number(outcome.score),
        reason: outcome.reason == null ? null : String(outcome.reason),
      };
    }
    /* memory-ring row: decision/model/terminal_node_id instead of outcome */
    const decision = String(dto.decision || "none").toLowerCase();
    if (decision === "route" || decision === "block") {
      return {
        kind: decision,
        model: dto.model == null ? null : String(dto.model),
        reasoning: dto.reasoning == null ? null : String(dto.reasoning),
        node_id: dto.terminal_node_id == null ? null : String(dto.terminal_node_id),
        score: dto.score == null ? null : Number(dto.score),
        reason: null,
      };
    }
    return {kind: "miss", model: null, reasoning: null, node_id: null,
            score: null, reason: dto.error_class || dto.reason || null};
  }

  /* ── summary / meta ─────────────────────────────────────────────────── */
  function summaryHTML(result, outcome, options) {
    const kindLabel = outcome.kind === "route" ? tr("trace.outcome.route")
      : outcome.kind === "block" ? tr("trace.outcome.block")
        : tr("trace.outcome.miss");
    const parts = [`<span class="rtt-badge" data-kind="${outcome.kind}" data-role="kind">` +
      escHTML(kindLabel) + "</span>"];
    if (outcome.kind === "miss") {
      const reason = outcome.reason ? reasonLabel(outcome.reason) : "";
      parts.push(`<span class="rtt-miss-reason" data-role="miss-reason">` +
        (outcome.reason ? `<code>${escHTML(reason)}</code> ` : "") +
        `<span class="mut">${escHTML(String(outcome.reason || ""))}</span></span>`);
      if (!outcome.reason) parts.push(`<span class="mut">—</span>`);
    } else {
      if (outcome.model) {
        parts.push(`<span class="rtt-arrow" aria-hidden="true">→</span>` +
          `<b class="rtt-model" data-role="model">${escHTML(outcome.model)}</b>`);
      }
      if (outcome.reasoning) {
        parts.push(`<span class="rtt-reasoning" data-role="reasoning">` +
          escHTML(tr("trace.reasoning")) + " " +
          escHTML(reasonEffort(outcome.reasoning)) + "</span>");
      }
      if (outcome.kind === "block") {
        parts.push(`<span class="rtt-block">${escHTML(tr("trace.hit.block"))}</span>`);
      }
      if (outcome.node_id) {
        parts.push(`<code class="rtt-node" data-role="node">${escHTML(outcome.node_id)}</code>`);
      }
      if (outcome.score != null && outcome.score !== "") {
        parts.push(`<span class="rtt-score" data-role="score">` +
          escHTML(tr("trace.score")) + " " + escHTML(num(outcome.score)) + "</span>");
      }
    }
    const tree = options && options.tree ? options.tree : null;
    const treeText = tree
      ? `${escHTML(tree.name || tree.id)} · ${escHTML(result.tree_id || tree.id || "—")}`
      : escHTML(result.tree_id || "—");
    const timing = tr("trace.timing", {
      signal: result.signal_ms == null ? "—" : Math.round(Number(result.signal_ms)),
      decision: result.decision_ms == null ? "—" : Math.round(Number(result.decision_ms)),
      calls: result.nl_calls == null ? "—" : result.nl_calls,
    });
    return `<div class="rtt-summary">${parts.join("")}</div>
      <div class="rtt-meta">
        <span class="rtt-tree" data-role="tree-id" data-tree-id="${escHTML(result.tree_id || "")}">` +
      escHTML(tr("trace.tree")) + " " + treeText + `</span>
        <span class="rtt-rev" data-role="tree-revision">` +
      escHTML(tr("trace.treeRevision")) + " " + escHTML(shortRev(result.tree_revision)) + `</span>
        <span class="rtt-rev" data-role="snapshot-revision">` +
      escHTML(tr("trace.snapshotRevision")) + " " + escHTML(shortRev(result.snapshot_revision)) + `</span>
      </div>
      <div class="rtt-meta rtt-timing" data-role="timing">${escHTML(timing)}</div>`;
  }

  /* ── per-event bodies ───────────────────────────────────────────────── */
  function conditionBody(event) {
    const signal = event.signal;
    const rule = signal ? isRuleSignal(signal) : false;
    const conf = event.conf;
    const floor = event.conf_min;
    const pass = event.pass === true;
    const parts = [];
    if (signal) {
      parts.push(`<span class="rtt-cond">` +
        `<b>${escHTML(sigLabel(signal))}</b>` +
        `<span class="rtt-op">${escHTML(event.op || "")}</span>` +
        `<span class="rtt-value">${escHTML(valueLabel(signal, event.value) || "")}</span>` +
        `</span>`);
      if (rule) {
        parts.push(`<span class="rtt-det">${escHTML(tr("trace.condition.deterministic"))}</span>`);
      } else if (conf != null) {
        parts.push(barHTML(conf, floor, !pass) +
          `<span class="rtt-conf">${escHTML(tr("trace.condition.conf"))} ` +
          `${escHTML(num(conf))}${floor != null ? " ≥ " + escHTML(num(floor)) : ""}</span>`);
      }
    }
    parts.push(`<span class="rtt-tag ${pass ? "ok" : "cut"}">` +
      escHTML(pass ? tr("trace.condition.pass") : tr("trace.condition.fail")) + "</span>");
    return `<div class="rtt-event-body">${parts.join("")}</div>`;
  }

  function linesHTML(lines) {
    if (!Array.isArray(lines) || !lines.length) return "";
    const rows = lines.map(line => {
      const item = line && typeof line === "object" ? line : {};
      return `<tr class="rtt-line" data-signal="${escHTML(item.signal || "")}">` +
        `<td>${escHTML(sigLabel(item.signal))} ${escHTML(valueLabel(item.signal, item.value) || "")}</td>` +
        `<td class="mono">${escHTML(num(item.weight))}</td>` +
        `<td class="mono">${escHTML(num(item.c))}</td>` +
        `<td class="mono">${escHTML(String(item.m == null ? "—" : item.m))}</td>` +
        `<td class="mono">${escHTML(num(item.contrib))}</td></tr>`;
    }).join("");
    return `<div class="rtt-evidence-block"><div class="rtt-evidence-h">` +
      escHTML(tr("trace.weights")) + `</div><table class="rtt-lines">` +
      `<tr><th>${escHTML(tr("trace.lines.signal"))}</th><th>w</th><th>c</th><th>m</th>` +
      `<th>${escHTML(tr("trace.lines.contrib"))}</th></tr>${rows}</table></div>`;
  }

  function terminalBody(event) {
    const kind = String(event.kind || "").toLowerCase();
    const parts = [];
    if (kind === "block") {
      parts.push(`<span class="rtt-tag cut">${escHTML(tr("trace.hit.block"))}</span>`);
    } else if (event.model) {
      parts.push(`<span class="rtt-arrow" aria-hidden="true">→</span>` +
        `<b class="rtt-model">${escHTML(event.model)}</b>`);
      if (event.reasoning) {
        parts.push(`<span class="rtt-reasoning">${escHTML(tr("trace.reasoning"))} ` +
          `${escHTML(reasonEffort(event.reasoning))}</span>`);
      }
    }
    if (event.score != null) {
      const minScore = event.min_score != null ? Number(event.min_score) : null;
      const passed = minScore == null || Number(event.score) >= minScore;
      parts.push(barHTML(event.score, minScore, !passed) +
        `<span class="rtt-conf mono">` +
        escHTML(tr("trace.scoreOf", {score: num(event.score),
                                     min: minScore == null ? "—" : num(minScore)})) +
        "</span>");
    }
    if (!parts.length) parts.push(`<span class="mut">—</span>`);
    return `<div class="rtt-event-body">${parts.join("")}${linesHTML(event.lines)}</div>`;
  }

  function nlBody(event) {
    const candidates = Array.isArray(event.candidates)
      ? event.candidates.slice()
      : (event.probabilities && typeof event.probabilities === "object"
        ? Object.keys(event.probabilities) : []);
    const probabilities = event.probabilities && typeof event.probabilities === "object"
      ? event.probabilities : {};
    const selected = event.selected_key == null ? null : String(event.selected_key);
    const ranking = Array.isArray(event.ranking) ? event.ranking : [];
    const top = ranking.length ? String(ranking[0]) : null;
    const rows = candidates.map(key => {
      const raw = probabilities[key];
      const known = raw != null && Number.isFinite(Number(raw));
      const isSelected = selected != null && String(key) === selected;
      const isTop = top != null && String(key) === top;
      return `<li class="rtt-candidate" data-candidate="${escHTML(key)}"` +
        ` data-selected="${isSelected ? "true" : "false"}">` +
        `<span class="rtt-cand-key">${escHTML(key)}</span>` +
        barHTML(known ? Number(raw) : 0, null, false) +
        `<span class="rtt-prob" data-role="prob">${escHTML(known ? num(raw) : "—")}</span>` +
        (isTop ? `<span class="rtt-cand-tag">${escHTML(tr("trace.nl.top"))}</span>` : "") +
        (isSelected ? `<span class="rtt-cand-tag sel">${escHTML(tr("trace.nl.selected"))}</span>` : "") +
        "</li>";
    }).join("");
    const stats = `<div class="rtt-nl-stats">` +
      `<span data-role="p-top">${escHTML(tr("trace.nl.pTop"))} ${escHTML(num(event.p_top))}</span>` +
      `<span data-role="margin">${escHTML(tr("trace.nl.margin"))} ${escHTML(num(event.margin))}</span>` +
      `<span data-role="min-prob">${escHTML(tr("trace.nl.minProb", {value: num(event.min_prob)}))}</span>` +
      `<span data-role="min-margin">${escHTML(tr("trace.nl.minMargin", {value: num(event.min_margin)}))}</span>` +
      `<span data-role="duration">${escHTML(tr("trace.nl.duration", {
        value: event.duration_ms == null ? "—" : num(event.duration_ms),
      }))}</span>` +
      (event.nl_call ? `<span class="rtt-tag" data-role="nl-call">${escHTML(tr("trace.nl.call"))}</span>` : "") +
      "</div>";
    const reason = event.reason
      ? `<div class="rtt-nl-reason"><span class="rtt-reason">` +
        `${escHTML(reasonLabel(event.reason))}</span></div>`
      : "";
    let error = "";
    if (event.error != null && event.error !== "") {
      error = `<div class="rtt-nl-error">${escHTML(tr("trace.nl.error"))}：` +
        `<code>${escHTML(String(event.error))}</code></div>`;
    }
    const evidence = {
      input: event.input && typeof event.input === "object" ? event.input : {},
      model_info: event.model_info && typeof event.model_info === "object"
        ? event.model_info : {},
    };
    return `<div class="rtt-event-body rtt-nl">` +
      `<span class="rtt-nl-title">${escHTML(tr("trace.nl.title"))}</span>` +
      (rows ? `<ul class="rtt-candidates">${rows}</ul>` : "") +
      stats + reason + error +
      `<details class="rtt-evidence"><summary>${escHTML(tr("trace.nl.evidence"))}</summary>` +
      `<pre class="rtt-json">${escHTML(JSON.stringify(evidence, null, 1))}</pre></details>` +
      `</div>`;
  }

  function selectedChildBody(event) {
    const parts = [];
    if (event.selected_key != null) {
      parts.push(`<span class="rtt-conf">${escHTML(tr("trace.selected"))} ` +
        `<code>${escHTML(String(event.selected_key))}</code></span>`);
    }
    if (event.selected_node_id != null) {
      parts.push(`<span class="rtt-conf">${escHTML(tr("trace.selectedNode"))} ` +
        `<code>${escHTML(String(event.selected_node_id))}</code></span>`);
    }
    return `<div class="rtt-event-body">${parts.join("")}</div>`;
  }

  function enteredBody(event) {
    const kind = entryKindLabel(event.kind);
    return `<div class="rtt-event-body">` +
      `<span class="mut">${esc(kind || "—")}</span></div>`;
  }

  function otherwiseBody(event) {
    const parts = [`<span class="mut">${escHTML(
      event.event === "otherwise_hit" ? tr("trace.otherwiseHit") : tr("trace.otherwiseMiss"))}</span>`];
    return `<div class="rtt-event-body">${parts.join("")}</div>`;
  }

  function rawBody(event) {
    const raw = typeof event === "object" && event !== null
      ? JSON.stringify(event) : String(event);
    return `<div class="rtt-raw"><code>${escHTML(raw)}</code></div>`;
  }

  function eventBody(event) {
    switch (event.event) {
      case "condition": return conditionBody(event);
      case "hit":
      case "miss": return terminalBody(event);
      case "nl_choice": return nlBody(event);
      case "selected_child_miss": return selectedChildBody(event);
      case "otherwise_hit":
      case "otherwise_miss": return otherwiseBody(event);
      case "entered": return enteredBody(event);
      default: return rawBody(event);
    }
  }

  function eventHTML(event, index) {
    const valid = event && typeof event === "object";
    const name = valid && event.event != null ? String(event.event) : "invalid";
    const nodeId = valid && event.node_id != null ? String(event.node_id) : "";
    const reason = valid && event.reason != null ? String(event.reason) : "";
    const label = valid ? eventLabel(name) : tr("trace.invalidEvent");
    const superseded = valid && event.superseded === true;
    const head = `<span class="rtt-idx mono">${index + 1}</span>` +
      (nodeId ? `<code class="rtt-node">${escHTML(nodeId)}</code>` : "") +
      `<span class="rtt-event-label" data-role="event-label">${escHTML(label)}</span>` +
      (reason
        ? `<span class="rtt-reason" data-role="reason-label">${escHTML(reasonLabel(reason))}</span>`
        : "") +
      (superseded
        ? `<span class="rtt-superseded" data-role="superseded">` +
          `${escHTML(tr("trace.superseded"))}</span>`
        : "");
    return `<li class="rtt-event${superseded ? " is-superseded" : ""}"` +
      ` data-event="${escHTML(name)}"` +
      (nodeId ? ` data-node-id="${escHTML(nodeId)}"` : "") +
      (reason ? ` data-reason="${escHTML(reason)}"` : "") + ">" +
      `<div class="rtt-event-head">${head}</div>` +
      (valid ? eventBody(event) : rawBody(event)) + "</li>";
  }

  /* ── public render ──────────────────────────────────────────────────── */
  function renderRoutingTrace(container, result, options) {
    if (!container || typeof container !== "object") {
      throw new Error("SRTraceView.renderRoutingTrace: container required");
    }
    const data = result && typeof result === "object" ? result : {};
    const opts = options && typeof options === "object" ? options : {};
    const outcome = outcomeOf(data);
    const trace = Array.isArray(data.trace) ? data.trace : [];
    const events = trace.map(eventHTML).join("");
    container.innerHTML = `<div class="rtt" data-outcome="${outcome.kind}">` +
      summaryHTML(data, outcome, opts) +
      (trace.length
        ? `<div class="rtt-events-h">${escHTML(tr("trace.events"))}</div>` +
          `<ol class="rtt-events">${events}</ol>`
        : `<div class="rtt-empty">${escHTML(tr("trace.noTrace"))}</div>`) +
      "</div>";
    return container;
  }

  return {
    renderRoutingTrace: renderRoutingTrace,
    eventLabel: eventLabel,
    reasonLabel: reasonLabel,
    outcomeOf: outcomeOf,
  };
})();

if (typeof module === "object" && module.exports) module.exports = SRTraceView;
