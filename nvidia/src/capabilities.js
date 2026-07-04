/**
 * capabilities.js — Model capability classification + catalog enrichment
 * Dynamically fetches and classifies models from NVIDIA NIM catalog
 */

const LLM  = 'llm';    // integrate.api.nvidia.com
const GENAI = 'genai';  // ai.api.nvidia.com

// Base capability definitions - these are the known NVIDIA model types
const CAPABILITY_DEFS = {
  chat: {
    type: 'chat',
    input: ['text'],
    output: ['text'],
    capabilities: ['chat', 'completion'],
    endpoints: [{ path: '/v1/chat/completions', host: LLM, kind: 'chat' }],
    streaming: true,
  },
  vision_chat: {
    type: 'vision_chat',
    input: ['text', 'image'],
    output: ['text'],
    capabilities: ['chat', 'vision'],
    endpoints: [{ path: '/v1/chat/completions', host: LLM, kind: 'chat' }],
    streaming: true,
  },
  embedding: {
    type: 'embedding',
    input: ['text'],
    output: ['vector'],
    capabilities: ['embeddings'],
    endpoints: [{ path: '/v1/embeddings', host: LLM, kind: 'embeddings' }],
    streaming: false,
  },
  image: {
    type: 'image',
    input: ['text', 'image'],
    output: ['image'],
    capabilities: ['image_generation', 'image_to_image'],
    endpoints: [
      { path: '/v1/images/generations', host: GENAI, kind: 'openai_image' },
      { path: '/v1/infer', host: GENAI, kind: 'native_infer' }
    ],
    streaming: false,
  },
  rerank: {
    type: 'rerank',
    input: ['text'],
    output: ['scores'],
    capabilities: ['reranking'],
    endpoints: [{ path: '/v1/ranking', host: GENAI, kind: 'ranking' }],
    streaming: false,
  },
  asr: {
    type: 'asr',
    input: ['audio'],
    output: ['text'],
    capabilities: ['speech_recognition'],
    endpoints: [{ path: '/v1/audio/transcriptions', host: GENAI, kind: 'asr' }],
    streaming: true,
  },
  tts: {
    type: 'tts',
    input: ['text'],
    output: ['audio'],
    capabilities: ['text_to_speech'],
    endpoints: [{ path: '/v1/audio/speech', host: GENAI, kind: 'tts' }],
    streaming: true,
  },
  audio: {
    type: 'audio',
    input: ['text', 'audio'],
    output: ['audio'],
    capabilities: ['audio_generation', 'music_generation'],
    endpoints: [{ path: '/v1/genai', host: GENAI, kind: 'native_infer' }],
    streaming: false,
  },
  video: {
    type: 'video',
    input: ['text', 'image'],
    output: ['video'],
    capabilities: ['video_generation'],
    endpoints: [{ path: '/v1/infer', host: GENAI, kind: 'native_infer_async' }],
    streaming: false,
  },
  ocr: {
    type: 'ocr',
    input: ['image'],
    output: ['text'],
    capabilities: ['ocr'],
    endpoints: [{ path: '/v1/infer', host: GENAI, kind: 'native_infer' }],
    streaming: false,
  },
  parse: {
    type: 'parse',
    input: ['image', 'document'],
    output: ['text'],
    capabilities: ['document_parsing', 'vision'],
    endpoints: [{ path: '/v1/chat/completions', host: LLM, kind: 'chat' }],
    streaming: true,
  },
};

// Model classification rules - ordered by specificity (most specific first)
const CLASSIFICATION_RULES = [
  // Vision/Chat models with vision capabilities
  { patterns: ['vila', 'neva', '-vision', 'vision-', 'paligemma', 'kosmos', 'llava', 'florence', 'phi-3-vision', 'phi-3.5-vision', 'nvclip'], type: 'vision_chat' },

  // Code-specific models
  { patterns: ['code', 'codestral', 'starcoder', 'codegemma', 'deepseek-coder', 'qwen-coder'], type: 'chat', extraCaps: ['code_generation', 'code_completion'] },

  // Embedding models
  { patterns: ['embed', 'embedqa', 'nv-embed', 'bge-', 'e5-', 'gte-'], type: 'embedding' },

  // Image generation models
  { patterns: ['flux', 'sdxl', 'stable-diffusion', 'sd3', 'sd3.5', 'stable-diffusion-3', 'qwen-image', 'consistory', 'kandinsky', 'shuttle', 'playground', 'kolors', 'sana', 'lumina'], type: 'image' },

  // Reranking models
  { patterns: ['rerank', 'reranking', 'nv-rerank', 'bge-rerank'], type: 'rerank' },

  // ASR (Speech Recognition) models
  { patterns: ['parakeet', 'canary', 'whisper', 'asr', 'conformer', 'citrinet', 'nemo-asr'], type: 'asr' },

  // TTS (Text-to-Speech) models
  { patterns: ['magpie-tts', 'fastpitch', 'radtts', 'tts', 'text-to-speech', 'xtts', 'bark', 'valle', 'nemo-tts'], type: 'tts' },

  // Audio generation models
  { patterns: ['fugatto', 'audiogen', 'musicgen', 'audio2', 'audioldm', 'musiclm', 'audiocraft', 'encodec'], type: 'audio' },

  // Video generation models
  { patterns: ['cosmos', 'stable-video', 'svd', 'video', 'ltx', 'wan2', 'mochi', 'videocrafter', 'modelscope', 'videopoet', 'phenaki', 'make-a-video'], type: 'video' },

  // OCR models
  { patterns: ['ocr', 'ocdrnet', 'ocrnet', 'paddleocr', 'trocr'], type: 'ocr' },

  // Document parsing models
  { patterns: ['-parse', 'retriever-parse', 'layoutlm', 'donut', 'nougat'], type: 'parse' },

  // Default: chat model
  { patterns: [], type: 'chat' },
];

// Parameter definitions per capability type
const CAPABILITY_PARAMS = {
  chat: {
    required: ['model', 'messages'],
    optional: ['temperature', 'top_p', 'max_tokens', 'max_completion_tokens',
      'frequency_penalty', 'presence_penalty', 'stop',
      'stream', 'stream_options', 'seed',
      'logprobs', 'top_logprobs', 'logit_bias',
      'response_format', 'tools', 'tool_choice', 'tool_instances',
      'n', 'user'],
    nvidia: ['top_k', 'repetition_penalty', 'length_penalty',
      'min_p', 'frequency_penalty', 'presence_penalty',
      'guided_decoding_backend', 'guided_json', 'guided_regex',
      'guided_choice', 'guided_grammar', 'guided_whitespace_pattern'],
    defaults: { temperature: 1.0, top_p: 1.0, max_tokens: 1024 },
  },
  embedding: {
    required: ['model', 'input', 'input_type'],
    optional: ['encoding_format', 'dimensions', 'truncate'],
    nvidia: ['input_type', 'truncate'],
    defaults: { input_type: 'query', encoding_format: 'float' },
  },
  vision_chat: {
    required: ['model', 'messages'],
    optional: ['temperature', 'top_p', 'max_tokens', 'max_completion_tokens',
      'frequency_penalty', 'presence_penalty', 'stop',
      'stream', 'stream_options', 'seed',
      'logprobs', 'top_logprobs',
      'response_format', 'tools', 'tool_choice',
      'n', 'user', 'detail'],
    nvidia: ['top_k', 'repetition_penalty', 'length_penalty',
      'min_p', 'guided_decoding_backend', 'guided_json'],
    defaults: { temperature: 1.0, top_p: 1.0, max_tokens: 1024, detail: 'auto' },
  },
  parse: {
    required: ['model', 'messages'],
    optional: ['temperature', 'top_p', 'max_tokens', 'stream', 'stream_options', 'seed'],
    nvidia: ['top_k', 'repetition_penalty'],
    defaults: { temperature: 0.0, max_tokens: 4096 },
  },
  image: {
    required: ['model', 'prompt'],
    optional: ['negative_prompt', 'n', 'response_format', 'size', 'width', 'height', 'seed'],
    nvidia: ['steps', 'guidance_scale', 'strength', 'num_images', 'prompt_strength', 'cfg_scale', 'sampler', 'scheduler'],
    defaults: { width: 1024, height: 1024, steps: 30, guidance_scale: 7.5, n: 1 },
  },
  rerank: {
    required: ['model', 'query', 'documents'],
    optional: ['top_n', 'return_documents'],
    nvidia: ['top_n', 'return_documents'],
    defaults: { top_n: 10, return_documents: true },
  },
  asr: {
    required: ['model', 'file'],
    optional: ['language', 'response_format', 'temperature', 'prompt'],
    nvidia: ['language', 'response_format'],
    defaults: { language: 'en', response_format: 'json' },
  },
  tts: {
    required: ['model', 'input', 'voice'],
    optional: ['response_format', 'speed'],
    nvidia: ['response_format', 'speed'],
    defaults: { response_format: 'mp3', speed: 1.0 },
  },
  video: {
    required: ['model'],
    optional: ['prompt', 'image', 'seed', 'duration', 'fps', 'width', 'height'],
    nvidia: ['duration', 'fps', 'width', 'height', 'cfg_scale', 'steps', 'seed'],
    defaults: { duration: 4, fps: 8, width: 512, height: 512 },
  },
  audio: {
    required: ['model', 'prompt'],
    optional: ['duration', 'seed', 'output_format'],
    nvidia: ['duration', 'output_format', 'seed'],
    defaults: { duration: 10, output_format: 'wav' },
  },
  ocr: {
    required: ['model', 'image'],
    optional: ['language', 'output_format'],
    nvidia: ['language', 'bounding_boxes'],
    defaults: { language: 'en' },
  },
};

// Context lengths are manually maintained as NIM upstream does not expose them.
// Default context window is applied in enrichModelMetadata if not specified here.
function getCapabilityParams(capType) {
  return CAPABILITY_PARAMS[capType] || CAPABILITY_PARAMS.chat;
}

const _classifyCache = new Map();
const _CLASSIFY_CACHE_MAX = 500;

/** Deep-clone a cached classify result so callers cannot corrupt the cache. */
function _cloneResult(obj) {
  return {
    ...obj,
    input: obj.input ? [...obj.input] : [],
    output: obj.output ? [...obj.output] : [],
    capabilities: obj.capabilities ? [...obj.capabilities] : [],
    endpoints: obj.endpoints ? obj.endpoints.map(ep => ({ ...ep })) : [],
    supported_params: obj.supported_params ? { ...obj.supported_params } : {},
  };
}

function classify(modelId) {
  const mid = (modelId || '').toLowerCase();
  const cached = _classifyCache.get(mid);
  if (cached) return _cloneResult(cached);

  // Find matching classification rule
  // The last rule has patterns:[] which acts as a catch-all fallback.
  for (const rule of CLASSIFICATION_RULES) {
    if (rule.patterns.length === 0 || rule.patterns.some(p => mid.includes(p.toLowerCase()))) {
      const baseDef = CAPABILITY_DEFS[rule.type] || CAPABILITY_DEFS.chat;
      const result = {
        id: modelId,
        source: 'heuristic',
        ...baseDef,
        // Deep-copy mutable arrays/objects from baseDef to avoid shared refs
        input: [...(baseDef.input || [])],
        output: [...(baseDef.output || [])],
        capabilities: [...(baseDef.capabilities || [])],
        endpoints: (baseDef.endpoints || []).map(ep => ({ ...ep })),
        supported_params: { ...getCapabilityParams(rule.type) },
      };

      // Add extra capabilities if specified
      if (rule.extraCaps) {
        result.capabilities = [...new Set([...result.capabilities, ...rule.extraCaps])];
      }

      // Add code capabilities if model name indicates code
      if (mid.includes('code') || mid.includes('coder') || mid.includes('codestral') || mid.includes('starcoder')) {
        result.capabilities = [...new Set([...result.capabilities, 'code_generation', 'code_completion'])];
      }
      
      // Context window overrides
      if (mid.includes('minimax-m3') || mid.includes('minimax-m2.7')) {
        result.context_window = 1000000;
      } else if (mid.includes('phi-3-vision-128k') || mid.includes('128k') || mid.includes('128b')) {
        result.context_window = 128000;
      } else if (mid.includes('32k') || mid.includes('nemotron-4-340b')) {
        result.context_window = 32768;
      } else if (mid.includes('llama-3.1') || mid.includes('llama-3.2') || mid.includes('llama-3.3')) {
        result.context_window = 131072;
      }

      // Evict oldest entry if cache exceeds limit
      if (_classifyCache.size >= _CLASSIFY_CACHE_MAX) {
        const firstKey = _classifyCache.keys().next().value;
        if (firstKey !== undefined) _classifyCache.delete(firstKey);
      }
      _classifyCache.set(mid, result);
      return _cloneResult(result);
    }
  }

  // NOTE: This fallback is unreachable because the last CLASSIFICATION_RULES entry
  // has patterns:[] which matches everything. Kept for defensive safety.
  const baseDef = CAPABILITY_DEFS.chat;
  const result = {
    id: modelId,
    source: 'heuristic',
    ...baseDef,
    input: [...(baseDef.input || [])],
    output: [...(baseDef.output || [])],
    capabilities: [...(baseDef.capabilities || [])],
    endpoints: (baseDef.endpoints || []).map(ep => ({ ...ep })),
    supported_params: { ...getCapabilityParams('chat') },
  };

  if (mid.includes('code') || mid.includes('coder')) {
    result.capabilities = [...new Set([...result.capabilities, 'code_generation', 'code_completion'])];
  }
  
  // Context window overrides for fallback
  if (mid.includes('minimax-m3') || mid.includes('minimax-m2.7')) {
    result.context_window = 1000000;
  } else if (mid.includes('phi-3-vision-128k') || mid.includes('128k') || mid.includes('128b')) {
    result.context_window = 128000;
  } else if (mid.includes('32k') || mid.includes('nemotron-4-340b')) {
    result.context_window = 32768;
  } else if (mid.includes('llama-3.1') || mid.includes('llama-3.2') || mid.includes('llama-3.3')) {
    result.context_window = 131072;
  }

  if (_classifyCache.size >= _CLASSIFY_CACHE_MAX) {
    const firstKey = _classifyCache.keys().next().value;
    if (firstKey !== undefined) _classifyCache.delete(firstKey);
  }
  _classifyCache.set(mid, result);
  return _cloneResult(result);
}

// Curated models that might not be in the NVIDIA catalog but should be available
const CURATED_GENAI = [
  'nvidia/ai-synthetic-video-detector',
  // Image generation — FLUX family
  'black-forest-labs/flux.1-dev',
  'black-forest-labs/flux.1-schnell',
  'black-forest-labs/flux.1-kontext-dev',
  'black-forest-labs/flux.1-canny-dev',
  'black-forest-labs/flux.1-depth-dev',
  'black-forest-labs/flux.2-klein',
  // Image generation — Stability AI
  'stabilityai/stable-diffusion-3.5-large',
  // Image generation — Qwen
  'qwen/qwen-image',
  'qwen/qwen-image-edit',
  // Image generation — other
  'playgroundai/playground-v2.5-1024px-aesthetic',
  'consistory/consistory',
  'kandinsky-community/kandinsky-3',
  // Audio generation
  'nvidia/fugatto',
];

// Retired/unavailable models (kept minimal - most should be dynamically determined)
const RETIRED_MODELS = {
  // These are kept for backward compatibility but ideally should be auto-detected
};

function _resolveHosts(desc, baseLLM, baseGenai) {
  const hosts = { [LLM]: baseLLM.replace(/\/+$/, ''), [GENAI]: baseGenai.replace(/\/+$/, '') };
  for (const ep of (desc.endpoints || [])) {
    ep.base_url = hosts[ep.host] || baseLLM.replace(/\/+$/, '');
  }
  return desc;
}

function describe(modelId, baseLLM, baseGenai) {
  return _resolveHosts(classify(modelId), baseLLM, baseGenai);
}

function buildCatalog(cachedIds, baseLLM, baseGenai) {
  const seen = new Set();
  const out = [];

  // Add cached models from NVIDIA API
  for (const mid of cachedIds) {
    if (!mid || seen.has(mid)) continue;
    seen.add(mid);
    const d = describe(mid, baseLLM, baseGenai);
    if (mid in RETIRED_MODELS) d.availability = RETIRED_MODELS[mid];
    out.push(d);
  }

  // Add curated models
  for (const mid of CURATED_GENAI) {
    if (seen.has(mid)) continue;
    seen.add(mid);
    const d = describe(mid, baseLLM, baseGenai);
    d.source = 'curated';
    if (mid in RETIRED_MODELS) d.availability = RETIRED_MODELS[mid];
    out.push(d);
  }

  return out;
}

function summarize(catalog) {
  const byType = {};
  for (const d of catalog) {
    byType[d.type] = (byType[d.type] || 0) + 1;
  }
  return { total: catalog.length, by_type: byType };
}

module.exports = {
  LLM, GENAI, classify, describe, buildCatalog, summarize,
  getCapabilityParams, CAPABILITY_PARAMS, RETIRED_MODELS, CURATED_GENAI,
};