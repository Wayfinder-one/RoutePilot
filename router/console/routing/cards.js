/* RTCards · 节点卡片渲染（规格 §6.5）
   ------------------------------------------------------------------
   本任务只做只读卡片：start / question(choice|noul|score) / logic / condition / target。
   卡片只产出 DOM，不绑定事件；端口 / 槽位 / 条件 chip 全部带 canvas.js 的 data-* 契约：
     node: data-node-id, data-node-type
     port: data-port, data-node, data-port-id, data-port-dir, data-port-type
     slot: data-slot（槽位行与输入端口各一份）
   ------------------------------------------------------------------ */
(function (root) {
  'use strict';

  const RTCards = (function () {
    const esc = value => String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    const t = key => (typeof root.t === 'function' ? root.t(key) : key);
    const tf = (key, vars) => (typeof root.tf === 'function' ? root.tf(key, vars) : t(key));

    function icon(name, cls) {
      return '<svg class="rtc-ic' + (cls ? ' ' + cls : '') + '" aria-hidden="true"><use href="#ri-' + name + '"/></svg>';
    }

    function snippet(text, length) {
      const value = String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
      if (!value) return '';
      return value.length > length ? value.slice(0, length) + '…' : value;
    }

    function portHTML(node, port, options) {
      if (!port) return '';
      const opts = options || {};
      const kind = port.type === 'number' ? 'num' : 'bool';
      const side = opts.side || (port.dir === 'out' ? 'right' : 'left');
      return '<span class="rtc-port rtc-port--' + kind + ' rtc-port--' + side + '"' +
        ' data-port data-node="' + esc(node.id) + '"' +
        ' data-port-id="' + esc(port.id) + '"' +
        ' data-port-dir="' + esc(port.dir) + '"' +
        ' data-port-type="' + esc(port.type) + '"' +
        (port.slot ? ' data-slot="' + esc(port.id) + '"' : '') +
        ' title="' + esc(port.label + ' · ' + port.desc) + '"></span>';
    }

    function incomingEdge(graph, nodeId, portId) {
      return graph.edges.find(edge => edge.target === nodeId && edge.target_port === portId) || null;
    }

    function sourceLabel(graph, edge) {
      const source = RTGraph.findNode(graph, edge.source);
      const port = source ? RTGraph.portOf(source, edge.source_port, graph) : null;
      let label = edge.source_port;
      if (port) label = port.short && port.short !== 'true' ? port.short : port.id;
      return edge.source + ' · ' + label;
    }

    function sourceTitle(edge) {
      return edge.source + '.' + edge.source_port + ' → ' + edge.target + '.' + edge.target_port;
    }

    function conditionText(graph, edge) {
      const type = RTGraph.edgeType(edge, graph);
      const condition = edge.condition;
      if (type === 'boolean') {
        if (condition == null) return '= true';
        if (condition.op === 'is' && typeof condition.value === 'boolean') {
          return condition.value ? '= true' : '= false';
        }
        return t('cv.cond.invalid');
      }
      if (condition == null) return t('cv.cond.unset');
      const op = RTGraph.OP[condition.op];
      const value = Number(condition.value);
      if (!op || !Number.isFinite(value)) return t('cv.cond.invalid');
      return op + ' ' + value;
    }

    function slotRow(graph, node, port) {
      const edge = incomingEdge(graph, node.id, port.id);
      if (!edge) {
        return '<div class="rtc-slot is-empty rtc-slot--ghost" data-slot="' + esc(port.id) + '"' +
          ' title="' + esc(tf('cv.slot.addTitle', { slot: port.id })) + '">' +
          portHTML(node, port, { side: 'left' }) +
          '<span class="rtc-slot-id">' + esc(port.id) + '</span>' +
          '</div>';
      }
      const kind = RTGraph.edgeType(edge, graph);
      return '<div class="rtc-slot' + (kind === 'number' ? ' is-num' : '') + '"' +
        ' data-slot="' + esc(port.id) + '" data-edge="' + esc(edge.id) + '">' +
        portHTML(node, port, { side: 'left' }) +
        '<span class="rtc-slot-id">' + esc(port.id) + '</span>' +
        '<span class="rtc-slot-src" title="' + esc(sourceTitle(edge)) + '">' + esc(sourceLabel(graph, edge)) + '</span>' +
        '<button type="button" class="rtc-cond' + (kind === 'number' ? ' is-num' : '') + '"' +
        ' data-cond-edge="' + esc(edge.id) + '" title="' + esc(t('cv.card.editCondition')) + '"' +
        ' aria-label="' + esc(tf('cv.card.editConditionAria', { slot: port.id })) + '">' +
        esc(conditionText(graph, edge)) + '</button>' +
        '<span class="rtc-live" data-live="slot.' + esc(port.id) + '"></span>' +
        '</div>';
    }

    function visibleSlots(graph, node) {
      const slots = RTGraph.portsOf(node, graph).filter(port => port.slot);
      if (node.type === 'logic' && node.operator === 'not') {
        const occupied = slots.filter(port => port.occupied);
        return occupied.length ? occupied.slice(0, 1) : slots.slice(0, 1);
      }
      return slots;
    }

    function slotsHTML(graph, node) {
      const slots = visibleSlots(graph, node);
      if (!slots.length) return '';
      /* question / target 的多槽位是 OR 语义（规格 §2.6），卡片上明确标注。 */
      const note = (node.type === 'question' || node.type === 'target') && slots.length > 1
        ? '<div class="rtc-slots-note">' + esc(t('cv.slot.orNote')) + '</div>'
        : '';
      return '<div class="rtc-slots">' + slots.map(port => slotRow(graph, node, port)).join('') + '</div>' + note;
    }

    function header(node, options) {
      const opts = options || {};
      return '<div class="rtc-card-h">' +
        (opts.icon ? '<span class="rtc-ico">' + icon(opts.icon) + '</span>' : '') +
        (opts.symbol ? '<span class="rtc-card-sym">' + esc(opts.symbol) + '</span>' : '') +
        '<b class="rtc-title' + (opts.textTitle ? ' rtc-title--txt' : '') + '" title="' + esc(node.id) + '">' +
        esc(opts.title || node.id) + '</b>' +
        (opts.badge ? '<span class="rtc-badge">' + esc(opts.badge) + '</span>' : '') +
        '<span class="rtc-live" data-live="state"></span>' +
        (opts.tail || '') +
        '</div>';
    }

    function nodeWrap(node, typeClass, inner) {
      const extra = [];
      if (node.title) extra.push('data-title="' + esc(node.title) + '"');
      if (node.question_type) extra.push('data-question-type="' + esc(node.question_type) + '"');
      if (node.operator) extra.push('data-operator="' + esc(node.operator) + '"');
      return '<div class="rtc-node rtc-node--' + esc(node.type) + ' rtc-node--' + typeClass + '"' +
        ' data-node-id="' + esc(node.id) + '" data-node-type="' + esc(node.type) + '" ' +
        extra.join(' ') + '>' + inner + '</div>';
    }

    function startCard(graph, node) {
      const port = RTGraph.portsOf(node, graph).find(item => item.dir === 'out');
      return nodeWrap(node, 'start',
        '<div class="rtc-card-h rtc-start">' +
        '<span class="rtc-ico">' + icon('start') + '</span>' +
        '<span class="rtc-start-txt"><b>' + esc(t('cv.card.start')) + '</b>' +
        '<span>' + esc(t('cv.card.startOut')) + '</span></span>' +
        '<span class="rtc-live" data-live="state"></span>' +
        portHTML(node, port, { side: 'right' }) +
        '</div>');
    }

    function choiceCard(graph, node) {
      const ports = RTGraph.portsOf(node, graph);
      const byId = id => ports.find(port => port.id === id);
      const rows = (Array.isArray(node.options) ? node.options : []).filter(option => option && typeof option === 'object').map(option => {
        const selected = byId('selected.' + option.key);
        const prob = byId('prob.' + option.key);
        return '<div class="rtc-opt" data-optkey="' + esc(option.key) + '">' +
          '<div class="rtc-opt-l">' +
          '<span class="rtc-opt-n"><b>' + esc(option.label) + '</b>' +
          '<span class="rtc-opt-p" data-live="prob.' + esc(option.key) + '">—</span></span>' +
          '</div>' +
          '<div class="rtc-oports">' +
          '<span class="rtc-oport">' + portHTML(node, selected, { side: 'right' }) + '<i>' + esc(t('cv.card.selected')) + '</i></span>' +
          '<span class="rtc-oport">' + portHTML(node, prob, { side: 'right' }) + '<i>' + esc(t('cv.card.prob')) + '</i></span>' +
          '</div></div>';
      }).join('');
      const topPort = byId('p_top');
      const marginPort = byId('margin');
      return nodeWrap(node, 'choice',
        header(node, { icon: 'question', badge: t('cv.card.choice') }) +
        '<div class="rtc-instr" title="' + esc(node.instructions || '') + '"><span class="rtc-instr-t">' + esc(snippet(node.instructions, 66)) + '</span></div>' +
        slotsHTML(graph, node) +
        '<div class="rtc-opts">' + rows + '</div>' +
        '<div class="rtc-qfoot">' +
        '<span class="rtc-sump">' + portHTML(node, topPort, { side: 'right' }) + '<i>p_top</i>' +
        '<b data-live="p_top">—</b></span>' +
        '<span class="rtc-sump">' + portHTML(node, marginPort, { side: 'right' }) + '<i>margin</i>' +
        '<b data-live="margin">—</b></span>' +
        '</div>');
    }

    function noulCard(graph, node) {
      const ports = RTGraph.portsOf(node, graph);
      const yes = ports.find(port => port.id === 'yes');
      const no = ports.find(port => port.id === 'no');
      return nodeWrap(node, 'noul',
        header(node, { icon: 'question', badge: t('cv.card.noul') }) +
        '<div class="rtc-instr" title="' + esc(node.instructions || '') + '"><span class="rtc-instr-t">' + esc(snippet(node.instructions, 66)) + '</span></div>' +
        slotsHTML(graph, node) +
        '<div class="rtc-qfoot rtc-qfoot--rows">' +
        '<span class="rtc-sump">' + portHTML(node, yes, { side: 'right' }) +
        '<i>yes · P(yes)</i><b data-live="yes">—</b></span>' +
        '<span class="rtc-sump">' + portHTML(node, no, { side: 'right' }) +
        '<i>no · 1-P(yes)</i><b data-live="no">—</b></span>' +
        '</div>');
    }

    function scoreCard(graph, node) {
      const value = RTGraph.portsOf(node, graph).find(port => port.id === 'value');
      const levels = Array.isArray(node.criteria) ? node.criteria.join(' / ') : '';
      return nodeWrap(node, 'score',
        header(node, { icon: 'question', badge: t('cv.card.score') }) +
        '<div class="rtc-instr" title="' + esc(node.instructions || '') + '"><span class="rtc-instr-t">' + esc(snippet(node.instructions, 66)) + '</span></div>' +
        slotsHTML(graph, node) +
        '<div class="rtc-qfoot rtc-qfoot--rows">' +
        '<span class="rtc-sump">' + portHTML(node, value, { side: 'right' }) +
        '<i>value</i><b data-live="value">—</b></span>' +
        (levels ? '<span class="rtc-criteria" title="' + esc(levels) + '">criteria ' + esc(levels) + '</span>' : '') +
        '</div>');
    }

    function logicCard(graph, node) {
      const ports = RTGraph.portsOf(node, graph);
      const output = ports.find(port => port.dir === 'out');
      const logic = RTGraph.LOGIC[node.operator] || { symbol: '?' };
      const key = 'cv.op.' + node.operator;
      const translated = t(key);
      const name = translated === key ? (logic.name || node.operator) : translated;
      return nodeWrap(node, 'logic',
        header(node, {
          symbol: logic.symbol,
          textTitle: true,
          title: String(name).split(' ')[0] || name,
          badge: name,
          tail: '<span class="rtc-gate" data-live="value"></span>' + portHTML(node, output, { side: 'right' })
        }) + slotsHTML(graph, node));
    }

    function conditionCard(graph, node) {
      const port = RTGraph.portsOf(node, graph).find(item => item.dir === 'out');
      const isText = node.source === 'text';
      const chips = isText
        ? (Array.isArray(node.contains_any) ? node.contains_any : [])
          .map(word => '<span class="rtc-kw" data-kw="' + esc(word) + '">' + esc(word) + '</span>').join('')
        : (Array.isArray(node.periods) ? node.periods : [])
          .map(period => '<span class="rtc-kw" data-period="' + esc(period) + '">' + esc(period) + '</span>').join('');
      return nodeWrap(node, 'condition',
        header(node, {
          icon: 'condition',
          textTitle: true,
          title: t('cv.card.condition'),
          badge: isText ? t('cv.card.text') : t('cv.card.periods')
        }) +
        '<div class="rtc-cond-body">' + (chips || '<span class="rtc-kw is-empty">' + esc(t('cv.card.noCondition')) + '</span>') + '</div>' +
        '<div class="rtc-cond-note">' + esc(t('cv.card.conditionAuto')) + '</div>' +
        portHTML(node, port, { side: 'right' }));
    }

    function targetCard(graph, node) {
      const models = (Array.isArray(node.models) ? node.models : [])
        .filter(item => item && typeof item === 'object')
        .map(item => {
          const reasoning = item && item.reasoning ? ' · ' + item.reasoning : '';
          return (item && item.model ? item.model : '—') + reasoning;
        });
      return nodeWrap(node, 'target',
        '<div class="rtc-card-h">' +
        '<span class="rtc-ico">' + icon('target') + '</span>' +
        '<b class="rtc-tname" title="' + esc(node.id) + '">' + esc(models[0] || node.id) + '</b>' +
        '<span class="rtc-prio">priority p' + esc(node.priority) + '</span>' +
        '</div>' +
        '<div class="rtc-tbody">' +
        '<div class="rtc-tmeta"><span>' + esc(models.join(' / ') || t('cv.card.noModels')) + '</span></div>' +
        '<div class="rtc-tmeta"><span>' + esc(node.strategy || 'least_inflight') + '</span>' +
        (node.capability ? '<span class="rtc-tcap-inline" title="' + esc(node.capability) + '">' + esc(snippet(node.capability, 38)) + '</span>' : '') +
        '</div>' +
        '</div>' +
        slotsHTML(graph, node) +
        '<div class="rtc-tfoot"><span class="rtc-tstate" data-live="state">' +
        esc(node.strategy || 'least_inflight') + ' · ' + esc(t('cv.card.noRun')) + '</span></div>');
    }

    function nodeCard(graph, node) {
      switch (node.type) {
        case 'start': return startCard(graph, node);
        case 'question':
          if (node.question_type === 'choice') return choiceCard(graph, node);
          if (node.question_type === 'score') return scoreCard(graph, node);
          return noulCard(graph, node);
        case 'logic': return logicCard(graph, node);
        case 'condition': return conditionCard(graph, node);
        case 'target': return targetCard(graph, node);
        default:
          return '<div class="rtc-node" data-node-id="' + esc(node.id) + '" data-node-type="' + esc(node.type || '') + '">' +
            esc(node.id) + '</div>';
      }
    }

    function render(graph, layer) {
      if (!layer) return 0;
      const nodes = graph && Array.isArray(graph.nodes) ? graph.nodes.filter(node => node && typeof node === 'object') : [];
      layer.innerHTML = nodes.map(node => nodeCard(graph, node)).join('');
      return nodes.length;
    }

    return {
      render: render,
      nodeCard: nodeCard,
      portHTML: portHTML,
      snippet: snippet,
      escape: esc
    };
  })();

  root.RTCards = RTCards;
  if (typeof module !== 'undefined' && module.exports) module.exports = RTCards;
})(typeof window !== 'undefined' ? window : globalThis);
