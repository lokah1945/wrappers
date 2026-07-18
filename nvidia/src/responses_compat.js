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

module.exports = function createResponsesHandler(deps) {
  const {
    pool, resolveTargetModel, proxyOpenai, forwardHeaders,
    BASE_LLM, BASE_GENAI, describe, CURATED_GENAI,
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
    const items = Array.isArray(input) ? input : (input ? [input] : []);
    for (const item of items) {
      if (!item || typeof item !== 'object') continue;
      const role = item.role;
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
        messages.push({ role: item.role || 'user', content: content ?? '' });
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

  function respondNonStreaming(res, data, model) {
    const msg = data.choices && data.choices[0] && data.choices[0].message;
    const text = (msg && msg.content) || '';
    const toolCalls = msg && msg.tool_calls;
    const respId = rand('resp');
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

    const result = await proxyOpenai(chatBody, forwardHeaders(req), model, req);
    if (!result.stream && result.status && result.status !== 200 && result.data) {
      return result.data; // error envelope from upstream (verbatim)
    }

    if (!chatBody.stream) {
      const data = result.data || {};
      return respondNonStreaming(res, data, model);
    }

    // Streaming: translate chat.completion.chunk SSE -> Responses SSE
    const respId = rand('resp');
    const msgId = rand('msg');
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
    emit({
      type: 'response.output_item.added', sequence_number: nextSeq(), output_index: 0,
      item: { id: msgId, type: 'message', status: 'in_progress', role: 'assistant', content: [] },
    });
    emit({
      type: 'response.content_part.added', sequence_number: nextSeq(),
      item_id: msgId, output_index: 0, content_index: 0,
      part: { type: 'output_text', text: '', annotations: [] },
    });

    let accText = '';
    // Per-tool-call accumulators keyed by OpenAI tool index so PARALLEL tool
    // calls are emitted as SEPARATE Responses function_call items. The previous
    // single `toolAcc` merged every delta into one name/args blob, producing a
    // single malformed function_call for parallel tools (Codex clients hang or
    // mis-invoke). Each entry gets a stable id/call_id generated at first sight.
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
              response_id: respId, item_id: msgId, output_index: 0, content_index: 0, delta: d.content,
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

    if (hasTool && toolAccs && toolAccs.length) {
      const toolItems = toolAccs.filter(Boolean).map((acc) => ({
        id: acc.id, type: 'function_call', status: 'completed', call_id: acc.callId,
        name: acc.name, arguments: acc.args,
      }));
      emit({
        type: 'response.output_text.done', sequence_number: nextSeq(),
        response_id: respId, item_id: msgId, output_index: 0, content_index: 0, text: accText,
      });
      emit({
        type: 'response.content_part.done', sequence_number: nextSeq(),
        item_id: msgId, output_index: 0, content_index: 0,
        part: { type: 'output_text', text: accText, annotations: [] },
      });
      emit({
        type: 'response.output_item.done', sequence_number: nextSeq(), output_index: 0,
        item: { id: msgId, type: 'message', status: 'completed', role: 'assistant', content: [{ type: 'output_text', text: accText, annotations: [] }] },
      });
      // Emit one function_call item PER parallel tool call (output_index 1..N).
      const outputs = [{ id: msgId, type: 'message', status: 'completed', role: 'assistant', content: [{ type: 'output_text', text: accText, annotations: [] }] }];
      toolItems.forEach((fcItem, i) => {
        emit({ type: 'response.output_item.added', sequence_number: nextSeq(), output_index: i + 1, item: fcItem });
        emit({ type: 'response.output_item.done', sequence_number: nextSeq(), output_index: i + 1, item: fcItem });
        outputs.push(fcItem);
      });
      emit({
        type: 'response.completed', sequence_number: nextSeq(),
        response: baseResponse(respId, model, 'completed', outputs, usage),
      });
    } else {
      emit({
        type: 'response.output_text.done', sequence_number: nextSeq(),
        response_id: respId, item_id: msgId, output_index: 0, content_index: 0, text: accText,
      });
      emit({
        type: 'response.content_part.done', sequence_number: nextSeq(),
        item_id: msgId, output_index: 0, content_index: 0,
        part: { type: 'output_text', text: accText, annotations: [] },
      });
      emit({
        type: 'response.output_item.done', sequence_number: nextSeq(), output_index: 0,
        item: { id: msgId, type: 'message', status: 'completed', role: 'assistant', content: [{ type: 'output_text', text: accText, annotations: [] }] },
      });
      emit({
        type: 'response.completed', sequence_number: nextSeq(),
        response: baseResponse(respId, model, 'completed',
          [{ id: msgId, type: 'message', status: 'completed', role: 'assistant', content: [{ type: 'output_text', text: accText, annotations: [] }] }], usage),
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
      const status = result.error.type === 'invalid_request_error' ? 400 : 502;
      res.writeHead(status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: result.error }));
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
