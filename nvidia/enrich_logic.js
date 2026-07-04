// NOTE: enrich_logic.js is a legacy twin of src/index.js#enrichModelMetadata.
// Kept for reference only — not imported by the running server.
// context length/window fields are intentionally omitted (NVIDIA NIM API
// forbids them in request payloads and does not return them in /v1/models).

function enrichModelMetadata(id, desc) {
  const isChat = desc.type === 'chat' || desc.type === 'vision_chat' || desc.type === 'parse';
  const isVision = desc.type === 'vision_chat' || desc.type === 'parse';
  return {
    id,
    object: 'model',
    owned_by: id.split('/')[0] || 'nvidia',
    created: 0,
    ...desc,
    supports_vision: isVision,
    supports_function_calling: isChat,
    supports_parallel_tool_calls: isChat,
    supports_streaming: desc.streaming !== false,
    supports_structured_output: isChat,
    supports_tool_choice: isChat,
    supports_stop_sequences: isChat,
    supports_system_prompt: isChat,
    supports_temperature: isChat,
    supports_top_p: isChat,
    supports_top_k: isChat,
    supports_seed: isChat,
    supports_logprobs: isChat && !desc.type?.includes('embedding'),
    supports_embedding: desc.type === 'embedding',
    supports_batch: false,
    provider: 'nvidia',
    model_family: id.includes('/') ? id.split('/')[1]?.split('-')[0] || id : id.split('-')[0] || id,
  };
}

module.exports = { enrichModelMetadata };
