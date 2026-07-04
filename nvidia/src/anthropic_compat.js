/**
 * anthropic_compat.js — Anthropic Messages API ⇄ OpenAI Chat Completions translation
 * Ported from Python anthropic_compat.py — functionally identical.
 *
 * Three translators:
 *  - anthropicToOpenai(body)               request  A→O
 *  - openaiToAnthropic(resp, model)        response O→A  (non-streaming)
 *  - streamOpenaiToAnthropic(stream, ...)  response O→A  (SSE async generator)
 */

const _FINISH_TO_STOP = {
  stop: 'end_turn',
  length: 'max_tokens',
  tool_calls: 'tool_use',
  // content_filter → refusal (Anthropic spec value for filtered responses) so
  // clients can distinguish a filtered response from a normal completion.
  content_filter: 'refusal',
  [null]: 'end_turn',
  [undefined]: 'end_turn',
};

function anthropicError(etype, message) {
  return { type: 'error', error: { type: etype, message } };
}

function _sse(event, data) {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

// ── Request: Anthropic → OpenAI ──────────────────────────────────────────

function _flattenText(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    // Concatenate all text blocks verbatim. Do NOT dedupe or insert newlines:
    // system prompts and tool_results legitimately contain multiple text blocks
    // (and repeated fragments) that must be preserved bit-for-bit for upstream
    // to see the same prompt the client sent. The previous dedup+newline logic
    // silently corrupted multi-block system prompts and tool_result content.
    return content
      .filter(b => typeof b === 'object' && b !== null && b.type === 'text')
      .map(b => b.text || '')
      .join('');
  }
  return '';
}

function anthropicToOpenai(a) {
  if (!a || typeof a !== 'object') return { model: '', messages: [] };
  const oai = { model: a.model || '' };
  const msgs = [];

  // system → leading system message
  const sys = a.system;
  const sysText = typeof sys === 'string' ? sys : _flattenText(sys);
  if (sysText) {
    msgs.push({ role: 'system', content: sysText });
  }

  const rawMessages = Array.isArray(a.messages) ? a.messages : [];
  for (const m of rawMessages) {
    if (!m || typeof m !== 'object') continue;
    const role = m.role;
    const content = m.content;

    if (typeof content === 'string') {
      msgs.push({ role, content });
      continue;
    }

    const parts = [];
    const toolCalls = [];
    const toolResults = [];

    const rawContent = Array.isArray(content) ? content : [];
    for (const blk of rawContent) {
      if (!blk || typeof blk !== 'object') continue;
      const t = blk.type;
      if (t === 'text') {
        parts.push({ type: 'text', text: blk.text || '' });
      } else if (t === 'thinking') {
        parts.push({ type: 'text', text: `<thinking>\n${blk.thinking || ''}\n</thinking>\n` });
      } else if (t === 'image') {
        const src = blk.source || {};
        let url;
        if (src.type === 'base64') {
          url = `data:${src.media_type || 'image/png'};base64,${src.data || ''}`;
        } else {
          url = src.url || '';
        }
        parts.push({ type: 'image_url', image_url: { url } });
      } else if (t === 'tool_use') {
        toolCalls.push({
          id: blk.id,
          type: 'function',
          function: {
            name: blk.name,
            arguments: JSON.stringify(blk.input || {}),
          },
        });
      } else if (t === 'tool_result') {
        let c = blk.content || '';
        c = Array.isArray(c) ? _flattenText(c) : c;
        toolResults.push({
          role: 'tool',
          tool_call_id: blk.tool_use_id,
          content: typeof c === 'string' ? c : JSON.stringify(c),
        });
      }
    }

    if (role === 'user') {
      // OpenAI wants tool results as their own role:"tool" messages first
      msgs.push(...toolResults);
      if (parts.length > 0) {
        if (parts.every(p => p.type === 'text')) {
          msgs.push({ role: 'user', content: parts.map(p => p.text).join('') });
        } else {
          msgs.push({ role: 'user', content: parts });
        }
      }
    } else if (role === 'assistant') {
      const am = { role: 'assistant' };
      const txt = parts.filter(p => p.type === 'text').map(p => p.text).join('');
      // OpenAI allows content=null only when tool_calls are present
      am.content = txt || (toolCalls.length > 0 ? null : '');
      if (toolCalls.length > 0) {
        am.tool_calls = toolCalls;
      }
      msgs.push(am);
    }
  }

  oai.messages = msgs;

  // Anthropic SDK requires max_tokens, but non-SDK clients may omit it. NIM
  // rejects or silently truncates (often to a tiny default) when max_tokens is
  // missing — truncation mid-tool-call corrupts the JSON in tool_calls.arguments.
  // Default to a sane value when omitted so the request is always well-formed.
  oai.max_tokens = a.max_tokens != null ? a.max_tokens : 4096;

  const paramMap = [
    ['temperature', 'temperature'],
    ['top_p', 'top_p'],
    ['top_k', 'top_k'],
    ['stop_sequences', 'stop'],
  ];
  for (const [src, dst] of paramMap) {
    if (a[src] != null) oai[dst] = a[src];
  }

  if (a.stream) oai.stream = true;

  if (a.tools && a.tools.length > 0) {
    oai.tools = a.tools.map(t => ({
      type: 'function',
      function: {
        name: t.name,
        description: t.description || '',
        parameters: t.input_schema || {},
      },
    }));
  }

  const tc = a.tool_choice;
  if (tc && typeof tc === 'object') {
    const tt = tc.type;
    if (tt === 'auto') oai.tool_choice = 'auto';
    else if (tt === 'any') oai.tool_choice = 'required';
    else if (tt === 'none') oai.tool_choice = 'none';
    else if (tt === 'tool') oai.tool_choice = { type: 'function', function: { name: tc.name } };
  }

  return oai;
}

// ── Token counting (approximate) ──────────────────────────────────────────

function estimateInputTokens(a) {
  if (!a || typeof a !== 'object') return 1;
  let chars = 0;
  const sys = a.system;
  chars += (typeof sys === 'string' ? sys : _flattenText(sys)).length;

  const rawMessages = Array.isArray(a.messages) ? a.messages : [];
  for (const m of rawMessages) {
    if (!m || typeof m !== 'object') continue;
    const c = m.content;
    if (typeof c === 'string') {
      chars += c.length;
    } else if (Array.isArray(c)) {
      for (const blk of c) {
        if (!blk || typeof blk !== 'object') continue;
        const t = blk.type;
        if (t === 'text') chars += (blk.text || '').length;
        else if (t === 'tool_use') chars += (blk.name || '').length + JSON.stringify(blk.input || {}).length;
        else if (t === 'tool_result') {
          const rc = blk.content || '';
          chars += (typeof rc === 'string' ? rc : JSON.stringify(rc)).length;
        } else if (t === 'image') {
          chars += 1600 * 4;
        }
      }
    }
  }

  const rawTools = Array.isArray(a.tools) ? a.tools : [];
  for (const t of rawTools) {
    if (!t || typeof t !== 'object') continue;
    chars += (t.name || '').length + (t.description || '').length + JSON.stringify(t.input_schema || {}).length;
  }

  return Math.max(1, Math.ceil(chars / 4));
}

// ── Response: OpenAI → Anthropic (non-streaming) ─────────────────────────

function openaiToAnthropic(o, model) {
  const choice = (o.choices?.length > 0 ? o.choices[0] : {});
  const msg = choice?.message || {};
  const content = [];

  let rawContent = msg.content || "";
  let reasoning = msg.reasoning_content || msg.reasoning || "";

  // Parse unstructured <think>...</think> if present in the content
  if (!reasoning && rawContent.startsWith("<think>")) {
    const endIdx = rawContent.indexOf("</think>");
    if (endIdx !== -1) {
      reasoning = rawContent.substring(7, endIdx).trim();
      rawContent = rawContent.substring(endIdx + 8).trim();
    }
  }

  if (reasoning) {
    content.push({ type: 'thinking', thinking: reasoning });
  }
  if (rawContent) {
    content.push({ type: 'text', text: rawContent });
  }
  for (const tc of (msg.tool_calls || [])) {
    const fn = tc.function || {};
    let args = {};
    try { args = JSON.parse(fn.arguments || '{}'); } catch {}
    content.push({ type: 'tool_use', id: tc.id, name: fn.name, input: args });
  }

  const u = o.usage || {};
  const cached = ((u.prompt_tokens_details) || {}).cached_tokens || 0;
  const usage = {
    input_tokens: u.prompt_tokens || 0,
    output_tokens: u.completion_tokens || 0,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: cached,
  };

  return {
    id: o.id || 'msg_wrapper',
    type: 'message',
    role: 'assistant',
    model,
    content,
    stop_reason: _FINISH_TO_STOP[choice.finish_reason] || 'end_turn',
    stop_sequence: null,
    usage,
  };
}

// ── Response: OpenAI SSE → Anthropic SSE (streaming) ─────────────────────

/**
 * Async generator: consume NVIDIA OpenAI SSE ReadableStream, emit Anthropic event stream.
 * Captures final usage/stop into `capture` for the caller's metrics.
 */
async function* streamOpenaiToAnthropic(stream, model, capture) {
  const msgId = 'msg_wrapper';
  let textIndex = null;
  let thinkingIndex = null;
  const toolMap = {};       // openai tool index -> anthropic block index
  let nextIndex = 0;
  let openIdx = null;
  let finalStop = 'end_turn';
  let usage = {};
  let inThinkTag = false;

  // Stop the currently-open block (if any) and reset its index so a future
  // delta for the SAME content type opens a FRESH block. Without these resets,
  // interleaved text→thinking→text or reasoning_content-after-</thinking>
  // patterns reuse an already-stopped block index → Claude Code SDK desync.
  const stopOpen = async function* () {
    if (openIdx !== null) {
      yield _sse('content_block_stop', { type: 'content_block_stop', index: openIdx });
      if (openIdx === textIndex) textIndex = null;
      if (openIdx === thinkingIndex) thinkingIndex = null;
      openIdx = null;
    }
  };

  const emitText = async function* (text) {
    if (textIndex === null) {
      yield* stopOpen();
      textIndex = nextIndex++;
      openIdx = textIndex;
      yield _sse('content_block_start', {
        type: 'content_block_start', index: textIndex,
        content_block: { type: 'text', text: '' },
      });
    }
    yield _sse('content_block_delta', {
      type: 'content_block_delta', index: textIndex,
      delta: { type: 'text_delta', text: text },
    });
  };

  const emitThinkingStart = async function* () {
    if (thinkingIndex === null) {
      yield* stopOpen();
      thinkingIndex = nextIndex++;
      openIdx = thinkingIndex;
      yield _sse('content_block_start', {
        type: 'content_block_start', index: thinkingIndex,
        content_block: { type: 'thinking', thinking: '' },
      });
    }
  };

  const emitThinkingDelta = async function* (text) {
    yield* emitThinkingStart();
    yield _sse('content_block_delta', {
      type: 'content_block_delta', index: thinkingIndex,
      delta: { type: 'thinking_delta', thinking: text },
    });
  };

  yield _sse('message_start', {
    type: 'message_start',
    message: {
      id: msgId, type: 'message', role: 'assistant', model,
      content: [], stop_reason: null, stop_sequence: null,
      usage: { input_tokens: 0, output_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
    },
  });
  yield _sse('ping', { type: 'ping' });

  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let isFirstRead = true;

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (isFirstRead) {
        capture.ttftMs = capture._startMs ? (Date.now() - capture._startMs) : 0;
        isFirstRead = false;
      }

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('data:')) continue;
        const data = trimmed.slice(5).trim();
        if (data === '[DONE]') continue;

        let chunk;
        try { chunk = JSON.parse(data); } catch { continue; }
        if (chunk.usage) usage = chunk.usage;

        const choices = chunk.choices || [];
        if (choices.length === 0) continue;
        const ch = choices[0];
        const delta = ch.delta || {};

        let contentText = delta.content || "";
        const reasoning = delta.reasoning_content || delta.reasoning;

        if (reasoning) {
          yield* emitThinkingDelta(reasoning);
        } else if (contentText) {
          if (!inThinkTag && contentText.includes("<think>")) {
            inThinkTag = true;
            const parts = contentText.split("<think>");
            const before = parts[0];
            const after = parts.slice(1).join("<think>");
            if (before) {
              yield* emitText(before);
            }
            yield* emitThinkingStart();
            contentText = after;
          }

          if (inThinkTag) {
            if (contentText.includes("</think>")) {
              inThinkTag = false;
              const parts = contentText.split("</think>");
              const inside = parts[0];
              const after = parts.slice(1).join("</think>");
              if (inside) {
                yield* emitThinkingDelta(inside);
              }
              if (openIdx !== null) {
                yield _sse('content_block_stop', { type: 'content_block_stop', index: openIdx });
                openIdx = null;
              }
              if (after) {
                yield* emitText(after);
              }
            } else {
              yield* emitThinkingDelta(contentText);
            }
          } else {
            yield* emitText(contentText);
          }
        }

        // tool-call deltas
        for (const tc of (delta.tool_calls || [])) {
          const oi = tc.index ?? 0;
          const fn = tc.function || {};
          if (!(oi in toolMap)) {
            if (openIdx !== null) {
              yield _sse('content_block_stop', { type: 'content_block_stop', index: openIdx });
            }
            textIndex = null;
            const ai = nextIndex++;
            toolMap[oi] = ai;
            openIdx = ai;
            // Generate unique tool call ID using message ID + index to prevent collisions
            // Use the tool call ID from OpenAI if available, otherwise generate a deterministic one
            // Generate a unique tool-call ID. The OpenAI id is preferred when
            // present; otherwise synthesize one with a random suffix so it is
            // globally unique across turns (the Anthropic SDK requires tool_use
            // ids to be unique so the client can reference them in the next
            // turn's tool_result). The previous `toolu_wrapper_${ai}` fallback
            // collided across turns because msgId is the constant 'msg_wrapper'.
            const toolCallId = tc.id || `toolu_${Math.random().toString(36).slice(2, 10)}${ai}`;
            yield _sse('content_block_start', {
              type: 'content_block_start', index: ai,
              content_block: {
                type: 'tool_use',
                id: toolCallId,
                name: fn.name || '',
                input: {},
              },
            });
          }
          const ai = toolMap[oi];
          if (fn.arguments) {
            yield _sse('content_block_delta', {
              type: 'content_block_delta', index: ai,
              delta: { type: 'input_json_delta', partial_json: fn.arguments },
            });
          }
        }

        if (ch.finish_reason) {
          finalStop = _FINISH_TO_STOP[ch.finish_reason] || 'end_turn';
        }
      }
    }
  } finally {
    try { reader.releaseLock(); } catch {}
  }

  if (openIdx !== null) {
    yield _sse('content_block_stop', { type: 'content_block_stop', index: openIdx });
  }

  yield _sse('message_delta', {
    type: 'message_delta',
    delta: { stop_reason: finalStop, stop_sequence: null },
    usage: {
      input_tokens: usage.prompt_tokens || 0,
      output_tokens: usage.completion_tokens || 0,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: ((usage.prompt_tokens_details) || {}).cached_tokens || 0,
    },
  });
  yield _sse('message_stop', { type: 'message_stop' });

  capture.usage = usage;
  capture.stop = finalStop;
}

module.exports = {
  anthropicToOpenai,
  openaiToAnthropic,
  streamOpenaiToAnthropic,
  estimateInputTokens,
  anthropicError,
};