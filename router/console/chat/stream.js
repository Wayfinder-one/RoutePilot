/* Chat 流式增量的增量 SSE 解码器 · 纯函数模块
   ------------------------------------------------------------------
   浏览器与 Node 共用：不读 DOM、不发请求、不持有定时器。
   push(text) 接收任意切分的 chunk（可在一行/事件中间截断），按空行
   边界拼出完整事件块；每个 data: 行按 SSE 规范用 \n 连接，空 payload
   直接忽略。end() 冲刷尾部残留块。
   [DONE] 是流正常终结的标记：调用可选的 onDone()，本模块不把它当 payload。
   有 onDone 时调用方可据此区分「正常结束」和「上游提前 EOF（未完成）」。
   JSON.parse 失败会抛错，由调用方的 fetch 循环决定如何收尾。
   ------------------------------------------------------------------ */

export function createSseParser(onPayload, onDone) {
  let buffer = '';

  function consume(block) {
    const data = block.split(/\r?\n/)
      .filter(line => line.startsWith('data:'))
      .map(line => line.slice(5).trimStart())
      .join('\n')
      .trim();
    if (!data) return;
    if (data === '[DONE]') {
      if (typeof onDone === 'function') onDone();
      return;
    }
    const payload = JSON.parse(data);
    onPayload(payload);
  }

  return {
    push(text) {
      buffer += String(text);
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop();
      blocks.forEach(consume);
    },
    end() {
      if (buffer.trim()) consume(buffer);
      buffer = '';
    },
  };
}
