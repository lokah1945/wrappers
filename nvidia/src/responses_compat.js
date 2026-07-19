'use strict';

// responses_compat.js — NATIVE OpenAI Responses API support for wrapper-nvidia.
//
// Codex >= 0.144 requires wire_api="responses" (the OpenAI Responses API).
// wrapper-nvidia is a pure NVIDIA NIM proxy, so this module translates the
// Responses request into a NIM chat/completions call and converts the
// response back into the Responses event format. No third-party routing.
//
// Models that are NOT in the NVIDIA NIM catalog cannot be served here — they
// must be used through their own provider, not through wrapper-nvidia.

function rand(suffix) {
  return suffix + '_' + Math.random().toString(36).slice(2, 12) + Date.now().toString(36).slice(-4);
}

// Map an OpenAI-style error envelope ({message,type,status?}) to the most
// faithful HTTP status. We prefer an upstream status carried on the error
// (proxyOpenai stamps it there) and otherwise fall back to the canonical
// mapping by error.type so client errors stay 4xx and server errors stay 5xx.
function httpStatusFromError(err) {
  if (err && typeof err.status === 'number') return err.status;
  const t = (err && err.type || '').toLowerCase();
  if (t === 'rate_limit_error') return 429;
  if (t === 'invalid_request_error') return 400;
  if (t === 'authentication_error') return 401;
  if (t === 'permission_error') return 403;
  if (t === 'not_found_error') return 404;
  if (t === 'request_too_large') return 413;
  if (t === 'unprocessable_entity_error') return 422;
  return 500;
}

module.exports = function createResponsesHandler(deps) {
  const {
    pool, resolveTargetModel, proxyOpenai, forwardHeaders,
    BASE_LLM, BASE_GENAI, describe, CURATED_GENAI,
    translateThinkingToNim,
    getDeprecatedRedirectInfo,
  } = deps;

  function isNvidiaModel(modelId) {
    // Source of truth = the live NVIDIA NIM catalog the wrapper learned.
    return Array.isArray(pool.modelsCached) &&
      (pool.modelsCached.includes(modelId) || (CURATED_GENAI || []).includes(modelId));
  }

  // ── Responses `input` -> OpenAI chat `messages` ──────────────────────────
  function inputToMessages(input, instructions) {
    const messages = [];
    if (instructions && typeof instructions === 'string' && instructions.length) {
      messages.push({ role: 'system', content: instructions });
    }
    // OpenAI Responses API accepts input as a bare string OR an array of
    // items. A bare string is the most common shape (one-shot prompt) and MUST
    // become a single user message. Wrapping a string into [input] and then
    // skipping non-object items yields an EMPTY messages array -> upstream NIM
    // rejects with 400 "messages field cannot be empty" -> wrapper surfaces
    // HTTP 502, breaking /v1/responses clients (Codex, Hermes). This is the
    // root cause of the Hermes ILMA / Codex 502 failure.
    const items = typeof input === 'string' ? [{ role: 'user', content: input }] :
                  Array.isArray(input) ? input : (input ? [input] : []);
    for (const item of items) {
      if (!item || typeof item !== 'object') {
        if (typeof item === 'string') messages.push({ role: 'user', content: item });
        continue;
      }
      const role = item.role;
      // FIX A-1 (Responses path): normalize `developer` -> `system`. NVIDIA
      // NIM chat templates reject a `developer` role (HTTP 500, especially
      // when chat_template_kwargs reasoning toggle is also sent). The OpenAI
      // Responses API treats `developer` as a privileged system-equivalent
      // role, so we map it to `system` and keep the content verbatim.
      const normalizedRole = (role === 'developer') ? 'system' : role;
      if (item.type === 'message' || ['user', 'system', 'developer', 'assistant'].includes(role)) {
        let content = item.content;
        if (Array.isArray(content)) {
          content = content.map((part) => {
            if (!part || typeof part !== 'object') return { type: 'text', text: '' };
            if (part.type === 'input_text') return { type: 'text', text: part.text || '' };
            if (part.type === 'input_image') {
              const url = part.image_url?.url || part.image_url || part.url;
              return { type: 'image_url', image_url: { url } };
            }
            if (part.type === 'input_file') return { type: 'text', text: part.text || '' };
            return { type: 'text', text: '' };
          });
        }
        messages.push({ role: normalizedRole || 'user', content: content ?? '' });
      } else if (item.type === 'function_call') {
        messages.push({
          role: 'assistant',
          content: null,
          tool_calls: [{
            id: item.call_id || rand('call'),
            type: 'function',
            function: {
              name: item.name || '',
              arguments: typeof item.arguments === 'string' ? item.arguments : JSON.stringify(item.arguments || {}),
            },
          }],
        });
      } else if (item.type === 'function_call_output') {
        messages.push({
          role: 'tool',
          tool_call_id: item.call_id,
          content: typeof item.output === 'string' ? item.output : JSON.stringify(item.output ?? ''),
        });
      }
    }
    return messages;
  }

  function convertTools(tools) {
    if (!Array.isArray(tools)) return undefined;
    const out = [];
    for (const t of tools) {
      if (t && t.type === 'function' && t.function) {
        out.push({
          type: 'function',
          function: {
            name: t.function.name,
            description: t.function.description || '',
            parameters: t.function.parameters || {},
          },
        });
      }
    }
    return out.length ? out : undefined;
  }

  function convertUsage(u) {
    if (!u) return undefined;
    return {
      input_tokens: u.prompt_tokens || 0,
      output_tokens: u.completion_tokens || 0,
      total_tokens: u.total_tokens || (u.prompt_tokens || 0) + (u.completion_tokens || 0),
    };
  }

  function baseResponse(id, model, status, output, usage) {
    return {
      id, object: 'response', created_at: Math.floor(Date.now() / 1000),
      model, status, output: output || [], usage: usage || null,
    };
  }

  // OpenAI Responses `reasoning` item (parity with Anthropic /v1/messages
  // `thinking` blocks). NIM exposes reasoning via `reasoning_content` (and
  // sometimes a final `reasoning` field); we surface it as a first-class
  // Responses `reasoning` item so Codex (wire_api="responses") does not lose
  // the semantic content that the Claude Code path keeps.
  function makeReasoningItem(text) {
    return { id: rand('rsn'), type: 'reasoning', status: 'completed', summary: '', text };
  }

  function respondNonStreaming(res, data, model) {
    const msg = data.choices && data.choices[0] && data.choices[0].message;
    const text = (msg && msg.content) || '';
    const toolCalls = msg && msg.tool_calls;
    const respId = rand('resp');
    // Preserve NIM structured reasoning so the Responses path is consistent
    // with the Anthropic /v1/messages path (which surfaces upstream reasoning
    // as a `thinking` block).
    const reasonRaw = msg && (msg.reasoning_content || msg.reasoning);
    const reasonText = typeof reasonRaw === 'string' ? reasonRaw : '';
    let output;
    if (toolCalls && toolCalls.length) {
      output = toolCalls.map((tc) => ({
        id: rand('fc'), type: 'function_call', status: 'completed',
        call_id: tc.id || rand('call'), name: tc.function?.name || '',
        arguments: tc.function?.arguments || '',
      }));
    } else {
      output = [{
        id: rand('msg'), type: 'message', status: 'completed', role: 'assistant',
        content: [{ type: 'output_text', text, annotations: [] }],
      }];
    }
    // Reasoning item leads the output array (index 0) when present.
    if (reasonText) output.unshift(makeReasoningItem(reasonText));
    return {
      id: respId, object: 'response', created_at: Math.floor(Date.now() / 1000),
      model: data.model || model, status: 'completed', output,
      usage: convertUsage(data.usage),
    };
  }

  // ── NVIDIA-native path: Responses -> chat/completions -> Responses ───────
  async function translateToNim(req, res, body, model) {
    const chatBody = {
      model,
      messages: inputToMessages(body.input, body.instructions),
      stream: !!body.stream,
    };
    if (body.temperature !== undefined) chatBody.temperature = body.temperature;
    if (body.top_p !== undefined) chatBody.top_p = body.top_p;
    if (body.max_output_tokens !== undefined) chatBody.max_tokens = body.max_output_tokens;
    else if (body.max_tokens !== undefined) chatBody.max_tokens = body.max_tokens;
    const tools = convertTools(body.tools);
    if (tools) chatBody.tools = tools;
    if (body.tool_choice && typeof body.tool_choice === 'object') {
      if (body.tool_choice.type === 'function' && body.tool_choice.name) {
        chatBody.tool_choice = { type: 'function', function: { name: body.tool_choice.name } };
      } else if (body.tool_choice.type === 'required') {
        chatBody.tool_choice = 'required';
      } else if (body.tool_choice.type === 'auto') {
        chatBody.tool_choice = 'auto';
      }
    }

    // Translate the OpenAI Responses `reasoning` control (Codex / OpenAI SDK)
    // into the model-specific NIM toggle, using the SAME single-source logic the
    // Anthropic path uses. Without this, reasoning models (deepseek-v4-pro,
    // qwen3-thinking, glm, etc.) reached via /v1/responses never think and
    // some HANG with no response. Client-provided chat_template_kwargs /
    // reasoning_effort in extra_body always win inside translateThinkingToNim.
    if (body && body.reasoning !== undefined) {
      translateThinkingToNim(chatBody, model, body.reasoning);
    }

    const result = await proxyOpenai(chatBody, forwardHeaders(req), model, req);
    // Non-stream error: proxyOpenai returns { status, data: {error:{...}} }.
    // Surface the upstream error envelope verbatim so the caller maps it to
    // the correct HTTP status (faithful 4xx/5xx, not a blanket 502).
    if (!result.stream && result.status && result.status !== 200 && result.data && result.data.error) {
      return { error: Object.assign({}, result.data.error, { status: result.status }) };
    }

    if (!chatBody.stream) {
      const data = result.data || {};
      return respondNonStreaming(res, data, model);
    }

    // Streaming: translate chat.completion.chunk SSE -> Responses SSE
    const respId = rand('resp');
    const msgId = rand('msg');
    const rsnId = rand('rsn');
    const RSN_INDEX = 0;     // reasoning item
    const MSG_INDEX = 1;     // assistant message item
    const seq = { n: 0 };
    const nextSeq = () => ++seq.n;
    const emit = (obj) => {
      if (res.writableEnded || res.destroyed) return;
      try { res.write(`data: ${JSON.stringify(obj)}\n\n`); } catch {}
    };

    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
      'X-Accel-Buffering': 'no',
    });
    res.on('error', () => {});

    const base = baseResponse(respId, model, 'in_progress');
    emit({ type: 'response.created', sequence_number: nextSeq(), response: base });
    emit({ type: 'response.in_progress', sequence_number: nextSeq(), response: base });

    // Reasoning item (index 0). Emitted lazily: only opened once the first
    // NIM reasoning_content/reasoning delta actually arrives, so we never
    // leave a dangling output_item.added (added but never completed).
    let rsnStarted = false;
    let accReason = '';
    const openReasoning = () => {
      rsnStarted = true;
      emit({
        type: 'response.output_item.added', sequence_number: nextSeq(), output_index: RSN_INDEX,
        item: { id: rsnId, type: 'reasoning', status: 'in_progress', summary: '', content: [] },
      });
      emit({
        type: 'response.reasoning_text.delta', sequence_number: nextSeq(),
        item_id: rsnId, output_index: RSN_INDEX, content_index: 0, delta: '',
      });
    };

    // Message item (index 1) — distinct index from the reasoning item so the
    // two never collide (the prior edit reused output_index 0 for both).
    emit({
      type: 'response.output_item.added', sequence_number: nextSeq(), output_index: MSG_INDEX,
      item: { id: msgId, type: 'message', status: 'in_progress', role: 'assistant', content: [] },
    });
    emit({
      type: 'response.content_part.added', sequence_number: nextSeq(),
      item_id: msgId, output_index: MSG_INDEX, content_index: 0,
      part: { type: 'output_text', text: '', annotations: [] },
    });

    let accText = '';
    // Per-tool-call accumulators keyed by OpenAI tool index so PARALLEL tool
    // calls are emitted as SEPARATE Responses function_call items. Each entry
    // gets a stable id/call_id generated at first sight.
    let toolAccs = null;
    let hasTool = false;
    let usage = null;
    const reader = result.stream.getReader();
    const decoder = new TextDecoder();
    try {
      while (true) {
        if (res.writableEnded || res.destroyed) break;
        const { done, value } = await reader.read();
        if (done) break;
        const chunkStr = decoder.decode(value, { stream: true });
        for (const line of chunkStr.split('\n')) {
          const t = line.trim();
          if (!t.startsWith('data:')) continue;
          const payload = t.slice(5).trim();
          if (payload === '[DONE]') continue;
          let c;
          try { c = JSON.parse(payload); } catch { continue; }
          if (c.usage) usage = convertUsage(c.usage);
          const d = c.choices && c.choices[0] && c.choices[0].delta;
          if (!d) continue;
          if (typeof d.content === 'string' && d.content) {
            accText += d.content;
            emit({
              type: 'response.output_text.delta', sequence_number: nextSeq(),
              response_id: respId, item_id: msgId, output_index: MSG_INDEX, content_index: 0, delta: d.content,
            });
          }
          // NIM reasoning_content/reasoning -> Responses reasoning item for
          // parity with Anthropic /v1/messages thinking blocks.
          const reasonDelta = (typeof d.reasoning_content === 'string' && d.reasoning_content)
            ? d.reasoning_content : ((typeof d.reasoning === 'string' && d.reasoning) ? d.reasoning : '');
          if (reasonDelta) {
            if (!rsnStarted) openReasoning();
            accReason += reasonDelta;
            emit({
              type: 'response.reasoning_text.delta', sequence_number: nextSeq(),
              item_id: rsnId, output_index: RSN_INDEX, content_index: 0, delta: reasonDelta,
            });
          }
          if (Array.isArray(d.tool_calls)) {
            if (!toolAccs) toolAccs = [];
            for (const tc of d.tool_calls) {
              const idx = (typeof tc.index === 'number') ? tc.index : toolAccs.length;
              let acc = toolAccs[idx];
              if (!acc) {
                acc = { name: '', args: '', id: rand('fc'), callId: rand('call') };
                toolAccs[idx] = acc;
              }
              if (tc.function && tc.function.name) acc.name += tc.function.name;
              if (tc.function && tc.function.arguments) acc.args += tc.function.arguments;
              hasTool = true;
            }
          }
        }
      }
    } catch (e) {
      console.error('[responses:nim stream]', e && e.message);
    }

    const outputs = [];
    if (rsnStarted) {
      emit({
        type: 'response.reasoning_text.done', sequence_number: nextSeq(),
        item_id: rsnId, output_index: RSN_INDEX, content_index: 0, text: accReason,
      });
      emit({
        type: 'response.output_item.done', sequence_number: nextSeq(), output_index: RSN_INDEX,
        item: { id: rsnId, type: 'reasoning', status: 'completed', summary: '', text: accReason },
      });
      outputs.push(makeReasoningItem(accReason));
    }

    if (hasTool && toolAccs && toolAccs.length) {
      const toolItems = toolAccs.filter(Boolean).map((acc) => ({
        id: acc.id, type: 'function_call', status: 'completed', call_id: acc.callId,
        name: acc.name, arguments: acc.args,
      }));
      emit({
        type: 'response.output_text.done', sequence_number: nextSeq(),
        response_id: respId, item_id: msgId, output_index: MSG_INDEX, content_index: 0, text: accText,
      });
      emit({
        type: 'response.content_part.done', sequence_number: nextSeq(),
        item_id: msgId, output_index: MSG_INDEX, content_index: 0,
        part: { type: 'output_text', text: accText, annotations: [] },
      });
      emit({
        type: 'response.output_item.done', sequence_number: nextSeq(), output_index: MSG_INDEX,
        item: { id: msgId, type: 'message', status: 'completed', role: 'assistant', content: [{ type: 'output_text', text: accText, annotations: [] }] },
      });
      // Emit one function_call item PER parallel tool call (output_index 2..N).
      toolItems.forEach((fcItem, i) => {
        const oi = i + 2;
        emit({ type: 'response.output_item.added', sequence_number: nextSeq(), output_index: oi, item: fcItem });
        emit({ type: 'response.output_item.done', sequence_number: nextSeq(), output_index: oi, item: fcItem });
        outputs.push(fcItem);
      });
      outputs.push({ id: msgId, type: 'message', status: 'completed', role: 'assistant', content: [{ type: 'output_text', text: accText, annotations: [] }] });
      emit({
        type: 'response.completed', sequence_number: nextSeq(),
        response: baseResponse(respId, model, 'completed', outputs, usage),
      });
    } else {
      emit({
        type: 'response.output_text.done', sequence_number: nextSeq(),
        response_id: respId, item_id: msgId, output_index: MSG_INDEX, content_index: 0, text: accText,
      });
      emit({
        type: 'response.content_part.done', sequence_number: nextSeq(),
        item_id: msgId, output_index: MSG_INDEX, content_index: 0,
        part: { type: 'output_text', text: accText, annotations: [] },
      });
      emit({
        type: 'response.output_item.done', sequence_number: nextSeq(), output_index: MSG_INDEX,
        item: { id: msgId, type: 'message', status: 'completed', role: 'assistant', content: [{ type: 'output_text', text: accText, annotations: [] }] },
      });
      outputs.push({ id: msgId, type: 'message', status: 'completed', role: 'assistant', content: [{ type: 'output_text', text: accText, annotations: [] }] });
      emit({
        type: 'response.completed', sequence_number: nextSeq(),
        response: baseResponse(respId, model, 'completed', outputs, usage),
      });
    }
    try { res.end(); } catch {}
    return null;
  }

  // ── Entry point ──────────────────────────────────────────────────────────
  async function handleResponsesApi(req, res, rawBody) {
    let body;
    try {
      body = JSON.parse(rawBody);
    } catch (e) {
      return { error: { message: 'Invalid JSON in /v1/responses: ' + e.message, type: 'invalid_request_error' } };
    }
    if (!body || !body.model) {
      return { error: { message: 'Missing "model" in /v1/responses request', type: 'invalid_request_error' } };
    }
    const model = resolveTargetModel(body.model);

    // Fix D: clear error for renamed/deprecated ids (Responses route envelope).
    const depR = getDeprecatedRedirectInfo ? getDeprecatedRedirectInfo(body.model) : null;
    if (depR) {
      return { error: { message: `Model "${depR.from}" has been renamed to "${depR.to}" in the NVIDIA NIM catalog. Update your request to use "${depR.to}".`, type: 'invalid_request_error' } };
    }

    if (!isNvidiaModel(model)) {
      return {
        error: {
          message: `Model "${model}" is not a NVIDIA NIM model and cannot be served by wrapper-nvidia. Use a NVIDIA NIM model (e.g. nvidia/llama-3.3-nemotron-super-49b-v1). wrapper-nvidia is NVIDIA-NIM-only.`,
          type: 'invalid_request_error',
        },
      };
    }

    console.log(`[responses] model=${model} -> NVIDIA NIM (native translate)`);
    const result = await translateToNim(req, res, body, model);

    if (result === null) return null;            // already streamed
    if (result && result.error) {
      // Faithful HTTP status: prefer the upstream status carried on the error,
      // else derive from the normalized error.type so client errors stay 4xx
      // and server errors stay 5xx. The internal status field is stripped
      // before serialization.
      const err = result.error;
      const status = typeof err.status === 'number' ? err.status : httpStatusFromError(err);
      const { status: _omit, ...errorOut } = err;
      res.writeHead(status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: errorOut }));
      return null;
    }
    if (result && result.id) {                    // non-streaming Response object
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(result));
      return null;
    }
    return null;
  }

  return { handleResponsesApi };
};
