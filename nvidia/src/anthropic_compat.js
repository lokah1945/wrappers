/**
 * anthropic_compat.js — Anthropic Messages API ⇄ OpenAI Chat Completions translation
 * Ported from Python anthropic_compat.py — functionally identical.
 *
 * Three translators:
 *  - anthropicToOpenai(body)               request  A→O
 *  - openaiToAnthropic(resp, model)        response O→A  (non-streaming)
 *  - streamOpenaiToAnthropic(stream, ...)  response O→A  (SSE async generator)
 */

// Import authoritative context-window map from capabilities.js (single source of
// truth shared with index.js). Previously this file kept its own duplicate
// COMPAT_MODEL_CONTEXT_WINDOWS map that drifted independently.
const { MODEL_CONTEXT_WINDOWS, DEFAULT_CONTEXT_WINDOW, getContextWindow: getCapContextWindow } = require('./capabilities');

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

function isAnthropicMessageOrderValid(messages) {
  let hasToolResult = false;
  for (const msg of messages) {
    if (!msg || !msg.role) continue;
    if (msg.role === 'system') continue;
    if (msg.role === 'tool') {
      hasToolResult = true;
    } else if (hasToolResult && msg.role !== 'assistant') {
      console.log('[anthropic_compat] Invalid order: "tool" followed by "' + msg.role + '". Rejecting.');
      return false;
    }
  }
  return true;
}

// Context-window lookup for Anthropic translation path.
// Uses the shared MODEL_CONTEXT_WINDOWS from capabilities.js (single source of
// truth). Authoritative NGC registry value always wins over heuristic map.
function getCompatContextWindow(modelId, officialContext) {
  // Authoritative NGC registry value always wins over heuristic map.
  if (officialContext && officialContext.context > 0) return officialContext.context;
  return getCapContextWindow(modelId);
}

function hasToolResultBlock(msg) {
  if (!msg || !msg.content) return false;
  if (typeof msg.content === 'string') return false;
  if (Array.isArray(msg.content)) {
    return msg.content.some(blk => blk && blk.type === 'tool_result');
  }
  return false;
}

function anthropicToOpenai(a, officialContext) {
  console.log('[anthropicToOpenai] Called with:', JSON.stringify(a).slice(0, 500));
  if (!a || typeof a !== 'object') return { model: '', messages: [] };
  if (!Array.isArray(a.messages)) return { model: '', messages: [] };

  // Sliding window context pruning.
  // Uses authoritative NGC registry context when available; falls back to heuristic map.
  const contextLimit = getCompatContextWindow(a.model, officialContext);
  const maxAllowedTokens = Math.max(4000, contextLimit - (a.max_tokens || 4096) - 2000);
  let currentTokens = estimateInputTokens(a);
  if (currentTokens > maxAllowedTokens) {
    console.log(`[anthropic_compat] Context budget exceeded: estimated ${currentTokens} tokens, max allowed is ${maxAllowedTokens} for model ${a.model}. Pruning history...`);
    while (a.messages.length > 1 && currentTokens > maxAllowedTokens) {
      // Never prune a leading system prompt (or any system message) — dropping
      // it silently corrupts every subsequent turn's instructions.
      if (a.messages[0] && a.messages[0].role === 'system') break;
      a.messages.shift();
      while (a.messages.length > 0 && a.messages[0]) {
        if (a.messages[0].role === 'system') break;
        if (a.messages[0].role !== 'user' || hasToolResultBlock(a.messages[0]) || a.messages[0].role === 'tool') {
          a.messages.shift();
        } else {
          break;
        }
      }
      currentTokens = estimateInputTokens(a);
    }
    console.log(`[anthropic_compat] Pruned history. New estimated tokens: ${currentTokens}, messages count: ${a.messages.length}`);
  }

  // Validate message order before translation
  if (!isAnthropicMessageOrderValid(a.messages)) {
    console.log('[anthropicToOpenai] Invalid message order detected. Rejecting.');
    return {
      error: {
        type: 'invalid_request_error',
        message: 'Invalid message order: after a "tool" message, only "assistant" messages are allowed.',
      }
    };
  }

  const oai = { model: a.model || '' };
  const msgs = [];

  const systemTexts = [];
  const sys = a.system;
  const sysText = typeof sys === 'string' ? sys : _flattenText(sys);
  if (sysText) {
    systemTexts.push(sysText);
  }

  const rawMessages = Array.isArray(a.messages) ? a.messages : [];
  for (const m of rawMessages) {
    if (m && m.role === 'system') {
      const mText = typeof m.content === 'string' ? m.content : _flattenText(m.content);
      if (mText) systemTexts.push(mText);
    }
  }

  if (systemTexts.length > 0) {
    msgs.push({ role: 'system', content: systemTexts.join('\n\n') });
  }

  for (const m of rawMessages) {
    if (!m || typeof m !== 'object') continue;
    const role = m.role;
    const content = m.content;

    if (role === 'system') {
      continue;
    }

    // 1. If role is 'tool'
    if (role === 'tool') {
      const toolResultContent = Array.isArray(content) ? content : [];
      for (const blk of toolResultContent) {
        if (blk && blk.type === 'tool_result') {
          let c = blk.content || '';
          c = Array.isArray(c) ? _flattenText(c) : c;
          msgs.push({
            role: 'tool',
            tool_call_id: blk.tool_use_id,
            content: typeof c === 'string' ? c : JSON.stringify(c),
          });
        }
      }
      continue;
    }

    // 2. If content is a simple string
    if (typeof content === 'string') {
      msgs.push({ role, content });
      continue;
    }

    // 3. If content is an array of blocks
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
        parts.push({ type: 'text', text: `<think>\n${blk.thinking || ''}\n</think>\n` });
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
      if (toolResults.length > 0) {
        msgs.push(...toolResults);
      }
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
  // 8192 (was 4096) gives large tool_calls.arguments JSON room to complete
  // without mid-stream truncation. Claude Code's SDK always sends max_tokens,
  // so this only affects non-SDK clients.
  oai.max_tokens = a.max_tokens != null ? a.max_tokens : 8192;



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

  // Passthrough extra_body + nvext for NVIDIA-specific params through the
  // Anthropic path. Allows advanced agents to send chat_template_kwargs,
  // nvext, etc. via the Anthropic API without losing them in translation.
  if (a.extra_body && typeof a.extra_body === 'object') {
    oai.extra_body = { ...a.extra_body };
  }
  if (a.nvext && typeof a.nvext === 'object') {
    oai.nvext = { ...a.nvext };
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
        else if (t === 'thinking') chars += (blk.thinking || '').length;
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

function openaiToAnthropic(o, model, requestId = null, expectThinking = false) {
  const choice = (o.choices?.length > 0 ? o.choices[0] : {});
  const msg = choice?.message || {};
  const content = [];

  let rawContent = msg.content || "";
  let reasoning = msg.reasoning_content || msg.reasoning || "";

  // Parse unstructured <think>...</think> or <thinking>...</thinking> if present in the content
  if (!reasoning) {
    const trimmed = rawContent.trim();
    if (trimmed.startsWith("<think>")) {
      const endIdx = trimmed.indexOf("</think>");
      if (endIdx !== -1) {
        reasoning = trimmed.substring(7, endIdx).trim();
        rawContent = trimmed.substring(endIdx + 8).trim();
      }
    } else if (trimmed.startsWith("<thinking>")) {
      const endIdx = trimmed.indexOf("</thinking>");
      if (endIdx !== -1) {
        reasoning = trimmed.substring(10, endIdx).trim();
        rawContent = trimmed.substring(endIdx + 11).trim();
      }
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

  // Contract shim: if the client requested extended thinking but the model
  // produced no thinking/reasoning block, prepend a minimal synthetic
  // `thinking` block so Claude Code's content-ordering contract is satisfied.
  if (expectThinking && !content.some(c => c.type === 'thinking') && content.length > 0) {
    content.unshift({ type: 'thinking', thinking: '[Reasoning not supported by this model; responding directly.]' });
  }

  // GUARD: Anthropic SDK throws "model output must contain either output text
  // or tool calls" if content has no text or tool_use block. This triggers in
  // two scenarios:
  //   (a) content is completely empty ([]) — no text, thinking, or tool calls
  //   (b) content has ONLY a thinking block — reasoning-only response when
  //       max_tokens runs out mid-think, or model returns reasoning_content
  //       but empty/null content (observed with deepseek-v4-pro, deepseek-r1).
  // Fix: ensure at least one {type:'text'} or {type:'tool_use'} block always
  // exists so the SDK validation always passes.
  if (!content.some(c => c.type === 'text' || c.type === 'tool_use')) {
    content.push({ type: 'text', text: '' });
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
    id: requestId ? `msg_${requestId}` : (o.id || `msg_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`),
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
async function* streamOpenaiToAnthropic(stream, model, capture, inputTokens = 0, requestId = null, expectThinking = false) {
  const msgId = requestId ? `msg_${requestId}` : `msg_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
  let textIndex = null;
  let thinkingIndex = null;
  const toolMap = {};       // openai tool index -> anthropic block index
  let nextIndex = 0;
  let openIdx = null;
  let sentContentBlockStart = false;
  // Tracks whether at least one text or tool_use block was emitted in the stream.
  // The Anthropic SDK requires "output text OR tool calls" — a thinking-only
  // stream also triggers the "model output must contain..." SDK error.
  let sentTextOrToolBlock = false;
  let finalStop = 'end_turn';
  let usage = {};
  let inThinkTag = false;
  let completedThinking = false;
  let generatedChars = 0;
  // Tracks whether a REAL thinking block was produced by the upstream model.
  // When the client requested extended thinking (Claude Code sends
  // `thinking: {type:"enabled"}`) but the model yields no reasoning (most
  // NVIDIA chat models are not reasoning models), we must still emit a
  // `thinking` block FIRST so the Anthropic content-ordering contract holds
  // ("first content block must be a thinking block"). Otherwise Claude Code
  // aborts the session. The synthetic block preserves the real model output
  // (text/tool_use) and only pads the contract.
  let realThinkingEmitted = false;
  let syntheticThinkingEmitted = false;

  // Stop the currently-open block (if any) and reset its index so a future
  // delta for the SAME content type opens a FRESH block. Without these resets,
  // interleaved text→thinking→text or reasoning_content-after-</thinking>
  // patterns reuse an already-stopped block index → Claude Code SDK desync.
  const stopOpen = async function* () {
    if (openIdx !== null) {
      yield _sse('content_block_stop', { type: 'content_block_stop', index: openIdx });
      if (openIdx === textIndex) textIndex = null;
      if (openIdx === thinkingIndex) thinkingIndex = null;
      for (const [k, v] of Object.entries(toolMap)) {
        if (v === openIdx) { delete toolMap[k]; break; }
      }
      openIdx = null;
    }
  };

  const emitText = async function* (text) {
    // Once we emit text, thinking phase is completed
    completedThinking = true;
    // Contract shim: if extended thinking was requested but the model produced
    // no real thinking yet, emit a minimal synthetic thinking block BEFORE the
    // first text block so Claude Code's content-ordering requirement holds.
    if (expectThinking && !realThinkingEmitted && !syntheticThinkingEmitted) {
      yield* emitSyntheticThinking();
    }
    if (textIndex === null) {
      yield* stopOpen();
      textIndex = nextIndex++;
      openIdx = textIndex;
      sentContentBlockStart = true;
      sentTextOrToolBlock = true;
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
      realThinkingEmitted = true;
      sentContentBlockStart = true;
      yield _sse('content_block_start', {
        type: 'content_block_start', index: thinkingIndex,
        content_block: { type: 'thinking', thinking: '' },
      });
    }
  };

  // Emit a synthetic `thinking` block so clients that requested extended
  // thinking still receive a spec-compliant first content block when the
  // upstream model does not actually reason. Never emits more than once.
  const emitSyntheticThinking = async function* () {
    if (syntheticThinkingEmitted || realThinkingEmitted) return;
    syntheticThinkingEmitted = true;
    yield* stopOpen();
    thinkingIndex = nextIndex++;
    openIdx = thinkingIndex;
    yield _sse('content_block_start', {
      type: 'content_block_start', index: thinkingIndex,
      content_block: { type: 'thinking', thinking: '' },
    });
    yield _sse('content_block_delta', {
      type: 'content_block_delta', index: thinkingIndex,
      delta: { type: 'thinking_delta', thinking: '[Reasoning not supported by this model; responding directly.]' },
    });
    yield _sse('content_block_stop', { type: 'content_block_stop', index: thinkingIndex });
    completedThinking = true; // Set completedThinking = true after synthetic thinking completes
    if (openIdx === thinkingIndex) { openIdx = null; thinkingIndex = null; }
  };

  const emitThinkingDelta = async function* (text) {
    yield* emitThinkingStart();
    yield _sse('content_block_delta', {
      type: 'content_block_delta', index: thinkingIndex,
      delta: { type: 'thinking_delta', thinking: text },
    });
  };

  const parseAndEmit = async function* (chunk, isReasoning) {
    if (completedThinking) {
      isReasoning = false;
    }

    if (!isReasoning && inThinkTag && thinkingIndex !== null) {
      yield* stopOpen();
      inThinkTag = false;
      completedThinking = true;
    }

    if (isReasoning && !inThinkTag && thinkingIndex !== null) {
      yield* emitText(chunk);
      return;
    }
    if (isReasoning && !inThinkTag) {
      inThinkTag = true;
      yield* emitThinkingStart();
    }

    if (inThinkTag) {
      let endIdx = -1;
      let tagLen = 0;

      const end1 = chunk.indexOf("</think>");
      const end2 = chunk.indexOf("</thinking>");
      const end3 = chunk.indexOf("<｜DSML｜tool_calls>");

      if (end1 !== -1) {
        endIdx = end1;
        tagLen = 8;
      }
      if (end2 !== -1 && (endIdx === -1 || end2 < endIdx)) {
        endIdx = end2;
        tagLen = 11;
      }
      if (end3 !== -1 && (endIdx === -1 || end3 < endIdx)) {
        endIdx = end3;
        tagLen = 0;
      }

      if (endIdx !== -1) {
        const inside = chunk.substring(0, endIdx);
        const after = chunk.substring(endIdx + tagLen);

        if (inside) {
          yield* emitThinkingDelta(inside);
        }
        yield* stopOpen();
        inThinkTag = false;
        completedThinking = true; // Mark completed when end tag is hit

        if (after) {
          yield* parseAndEmit(after, false);
        }
      } else {
        yield* emitThinkingDelta(chunk);
      }
    } else {
      let startIdx = -1;
      let tagLen = 0;

      const start1 = chunk.indexOf("<think>");
      const start2 = chunk.indexOf("<thinking>");

      if (start1 !== -1) {
        startIdx = start1;
        tagLen = 7;
      }
      if (start2 !== -1 && (startIdx === -1 || start2 < startIdx)) {
        startIdx = start2;
        tagLen = 10;
      }

      if (startIdx !== -1) {
        const before = chunk.substring(0, startIdx);
        const after = chunk.substring(startIdx + tagLen);

        if (before) {
          yield* emitText(before);
        }
        inThinkTag = true;
        yield* emitThinkingStart();

        if (after) {
          yield* parseAndEmit(after, false);
        }
      } else {
        yield* emitText(chunk);
      }
    }
  };


  yield _sse('message_start', {
    type: 'message_start',
    message: {
      id: msgId, type: 'message', role: 'assistant', model,
      content: [], stop_reason: null, stop_sequence: null,
      usage: { input_tokens: inputTokens, output_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
    },
  });
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let lastUsageChunk = '';
  let isFirstRead = true;

  // Periodic heartbeat (ping) during long idle periods
  const HEARTBEAT_MS = parseInt(process.env.HEARTBEAT_INTERVAL_MS || '5000', 10);
  let heartbeatTimer = null;
  let lastDataTime = Date.now();

  const heartbeatPromise = () => new Promise(resolve => {
    const elapsed = Date.now() - lastDataTime;
    const wait = Math.max(0, HEARTBEAT_MS - elapsed);
    heartbeatTimer = setTimeout(() => resolve({ _heartbeat: true }), wait);
  });

  const clearHeartbeat = () => { if (heartbeatTimer) { clearTimeout(heartbeatTimer); heartbeatTimer = null; } };

  // Tracks whether the upstream read loop terminated due to an error (network
  // drop / abort) rather than a clean [DONE]. When set, we MUST NOT emit the
  // terminal message_delta/message_stop SSE events — doing so would leave the
  // stream well-formed-but-incomplete and Claude Code would silently accept a
  // truncated response. The caller (handleAnthropicMessages) inspects
  // capture.errored and emits a single `event: error` instead.
  let errored = false;
  let errorMessage = '';

  try {
    let pendingRead = reader.read();
    while (true) {
      const result = await Promise.race([pendingRead, heartbeatPromise()]);
      clearHeartbeat();
      if (result._heartbeat) {
        yield _sse('ping', { type: 'ping' });
        continue;
      }
      const { done, value } = result;
      if (done) break;
      lastDataTime = Date.now();
      pendingRead = reader.read();
      if (isFirstRead) {
        capture.ttftMs = capture._startMs ? (Date.now() - capture._startMs) : 0;
        isFirstRead = false;
      }

      const chunkText = decoder.decode(value, { stream: true });
      buffer += chunkText;
      if (chunkText.includes('"usage"')) lastUsageChunk = chunkText;
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('data:')) continue;
        const data = trimmed.slice(5).trim();
        if (data === '[DONE]') continue;

        let chunk;
        try { chunk = JSON.parse(data); } catch { continue; }
        if (chunk.usage && typeof chunk.usage === 'object' && Object.keys(chunk.usage).length > 0) {
          usage = chunk.usage;
          capture.usage = chunk.usage;
        }
        if (!chunk.choices || chunk.choices.length === 0) continue;

        const choices = chunk.choices || [];
        if (choices.length === 0) continue;
        const ch = choices[0];
        const delta = ch.delta || {};

        let contentText = delta.content || "";
        const reasoning = delta.reasoning_content || delta.reasoning;

        if (reasoning) {
          generatedChars += reasoning.length;
          yield* parseAndEmit(reasoning, true);
        }
        if (contentText) {
          generatedChars += contentText.length;
          yield* parseAndEmit(contentText, false);
        }

        // tool-call deltas
        for (const tc of (delta.tool_calls || [])) {
          const oi = tc.index ?? 0;
          const fn = tc.function || {};
          if (!(oi in toolMap)) {
            // Contract shim: ensure a thinking block precedes the first tool_use
            // block when extended thinking was requested but the model did not
            // actually reason.
            if (expectThinking && !realThinkingEmitted && !syntheticThinkingEmitted) {
              yield* emitSyntheticThinking();
            }
            yield* stopOpen();
            const ai = nextIndex++;
            toolMap[oi] = ai;
            openIdx = ai;
            sentContentBlockStart = true;
            // Generate unique tool call ID using message ID + index to prevent collisions
            // Use the tool call ID from OpenAI if available, otherwise generate a deterministic one
            // Generate a unique tool-call ID. The OpenAI id is preferred when
            // present; otherwise synthesize one with a random suffix so it is
            // globally unique across turns (the Anthropic SDK requires tool_use
            // ids to be unique so the client can reference them in the next
            // turn's tool_result). The previous `toolu_wrapper_${ai}` fallback
            // collided across turns because msgId is the constant 'msg_wrapper'.
            const toolCallId = tc.id || `toolu_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}_${ai}`;
            sentTextOrToolBlock = true;
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
            generatedChars += fn.arguments.length;
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
    // Process any remaining content in buffer after done
    if (buffer) {
      const remainingLines = buffer.split('\n');
      for (const line of remainingLines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('data:')) continue;
        const data = trimmed.slice(5).trim();
        if (data === '[DONE]') continue;
        try {
          const chunk = JSON.parse(data);
          if (chunk.usage && typeof chunk.usage === 'object' && Object.keys(chunk.usage).length > 0) {
            usage = chunk.usage;
            capture.usage = chunk.usage;
          }
        } catch {}
      }
    }
  } catch (e) {
    errored = true;
    errorMessage = e && e.message ? e.message : 'upstream connection error';
    const errCause = e?.cause ? (e.cause.message || e.cause.code || String(e.cause)) : 'none';
    console.error(`[stream generator] Upstream read error: name=${e?.name} message=${e?.message} cause=${errCause} sentContentBlockStart=${sentContentBlockStart} errored=${errored}`, (e?.stack || '').split('\n').slice(0, 3).join(' ← '));
  } finally {
    clearHeartbeat();
    // Defensive: some stream readers (e.g. test mocks, custom ReadableStream
    // transforms) may not implement cancel(). Swallow the TypeError so the
    // generator isn't killed prematurely — releaseLock below is sufficient.
    try {
      if (typeof reader.cancel === 'function') {
        reader.cancel().catch(() => {});
      }
    } catch {}
    try { reader.releaseLock(); } catch {}
    // If the read loop threw (upstream error / abort) BEFORE the normal
    // terminal-event path below could run, the stream would be left with an
    // open content_block (content_block_start with no matching
    // content_block_stop). Close any still-open block here so the SSE stream
    // stays well-formed even on the error path. The handler emits the
    // terminal `error` event; we only ensure block-level integrity.
    if (openIdx !== null) {
      try { yield _sse('content_block_stop', { type: 'content_block_stop', index: openIdx }); } catch {}
      openIdx = null;
    }

    // FIX B2: Correct capture.usage inside finally so it runs even when the
    // parent for-await-of loop breaks (res.writableEnded or client disconnect).
    // Without this, the estimatedOutput fallback below was skipped on early
    // return, leaving prompt_tokens=0 or completion_tokens=0 in the metrics.
    _finalizeCapture(capture, usage, inputTokens, generatedChars, lastUsageChunk, finalStop);
  }

  if (openIdx !== null) {
    yield _sse('content_block_stop', { type: 'content_block_stop', index: openIdx });
  }

  // Ensure heartbeat timer is cleared even on normal completion path
  clearHeartbeat();

  // On upstream error we stop here: emit NO message_delta/message_stop so the
  // stream is unambiguously terminated by the caller's `event: error`.
  if (errored) {
    capture.errored = true;
    capture.errorMessage = errorMessage || 'upstream connection error';
    return;
  }

  // GUARD: Anthropic SDK throws "model output must contain either output text
  // or tool calls" in two scenarios:
  //   (a) no content block was emitted at all (sentContentBlockStart=false)
  //   (b) ONLY thinking blocks were emitted with no text/tool_use block
  //       (sentTextOrToolBlock=false) — reasoning-only responses.
  // Emit a minimal empty text block to satisfy the SDK contract in both cases.
  if (!sentTextOrToolBlock && !errored) {
    const emptyIdx = nextIndex++;
    yield _sse('content_block_start', {
      type: 'content_block_start', index: emptyIdx,
      content_block: { type: 'text', text: '' },
    });
    yield _sse('content_block_stop', { type: 'content_block_stop', index: emptyIdx });
  }

  const estimatedOutput = Math.max(1, Math.ceil(generatedChars / 4));
  const reportedInput = usage.prompt_tokens || inputTokens || 0;
  const reportedOutput = usage.completion_tokens || estimatedOutput;
  // Stash the EXACT tokens we are about to emit to the client so the server's
  // metrics always match what Claude Code saw — even when NIM omits a usage
  // chunk (some big-context / reasoning requests return 200 with no usage,
  // which previously left completion_tokens=0 in the dashboard while the
  // client still received real text). This is the root-cause fix for the
  // "output_tokens=0 but prompt_tokens present" symptom.
  capture.reportedInputTokens = reportedInput;
  capture.reportedOutputTokens = reportedOutput;
  yield _sse('message_delta', {
    type: 'message_delta',
    delta: { stop_reason: finalStop, stop_sequence: null },
    usage: {
      input_tokens: reportedInput,
      output_tokens: reportedOutput,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: ((usage.prompt_tokens_details) || {}).cached_tokens || 0,
    },
  });
  yield _sse('message_stop', { type: 'message_stop' });
}

function _finalizeCapture(capture, usage, inputTokens, generatedChars, lastUsageChunk, finalStop) {
  if (!capture.usage || Object.keys(capture.usage).length === 0) {
    if (lastUsageChunk) {
      const usageIdx = lastUsageChunk.indexOf('"usage"');
      if (usageIdx !== -1) {
        const colonIdx = lastUsageChunk.indexOf(':', usageIdx);
        if (colonIdx !== -1) {
          let braceStart = lastUsageChunk.indexOf('{', colonIdx);
          if (braceStart !== -1) {
            let depth = 0;
            let braceEnd = -1;
            for (let i = braceStart; i < lastUsageChunk.length; i++) {
              if (lastUsageChunk[i] === '{') depth++;
              else if (lastUsageChunk[i] === '}') {
                depth--;
                if (depth === 0) { braceEnd = i; break; }
              }
            }
            if (braceEnd !== -1) {
              try {
                capture.usage = JSON.parse(lastUsageChunk.slice(braceStart, braceEnd + 1));
              } catch {}
            }
          }
        }
      }
    }
  }
  if (!capture.usage || Object.keys(capture.usage).length === 0) {
    capture.usage = { ...usage };
  }
  const estimatedOutput = Math.max(1, Math.ceil(generatedChars / 4));
  if (capture.usage) {
    const pt = capture.usage.prompt_tokens || capture.usage.input_tokens || 0;
    const ct = capture.usage.completion_tokens || capture.usage.output_tokens || 0;
    if (!pt) {
      if (capture.usage.prompt_tokens !== undefined) capture.usage.prompt_tokens = inputTokens;
      else if (capture.usage.input_tokens !== undefined) capture.usage.input_tokens = inputTokens;
      else capture.usage.prompt_tokens = inputTokens;
    }
    if (!ct) {
      if (capture.usage.completion_tokens !== undefined) capture.usage.completion_tokens = estimatedOutput;
      else if (capture.usage.output_tokens !== undefined) capture.usage.output_tokens = estimatedOutput;
      else capture.usage.completion_tokens = estimatedOutput;
    }
  }
  capture.stop = finalStop;
}

module.exports = {
  anthropicToOpenai,
  openaiToAnthropic,
  streamOpenaiToAnthropic,
  estimateInputTokens,
  anthropicError,
  _finalizeCapture,
};