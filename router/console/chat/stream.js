/* Chat 流式增量的 SSE 解码器与终态跟踪 · 纯函数模块
   ------------------------------------------------------------------
   浏览器与 Node 共用：不读 DOM、不发请求、不持有定时器。
   push(text) 接收任意切分的 chunk（可在一行/事件/JSON 中间截断），按空行
   边界拼出完整事件块；每个 data: 行按 SSE 规范用 \n 连接，空 payload 与
   注释行直接忽略。end() 冲刷尾部残留块，并返回是否观察到终态标记
   （[DONE] 或任一 choices[].finish_reason != null）——调用方据此区分
   「正常结束」（true）与「上游提前 EOF（未完成）」（false）。
   带 error 的 payload 抛 Error(message || JSON.stringify(error))，并把
   原始 error 挂在 .payload 上，由调用方的 fetch 循环决定如何收尾。
   JSON 解析失败同样上抛。
   ------------------------------------------------------------------ */

export function createSseParser(onPayload) {
  let buffer = '';
  let terminal = false;

  function consume(block) {
    const data = block.split(/\r?\n/)
      .filter(line => line.startsWith('data:'))
      .map(line => line.slice(5).trimStart())
      .join('\n')
      .trim();
    if (!data) return;
    if (data === '[DONE]') { terminal = true; return; }
    const payload = JSON.parse(data);
    if (payload.error) {
      const error = new Error(payload.error.message || JSON.stringify(payload.error));
      error.payload = payload.error;
      throw error;
    }
    if ((payload.choices || []).some(choice => choice.finish_reason != null)) terminal = true;
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
      return terminal;
    },
  };
}
