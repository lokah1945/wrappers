/**
 * capabilities.js — Model capability classification + catalog enrichment
 * Ported from Python capabilities.py — functionally identical.
 */

const LLM  = 'llm';    // integrate.api.nvidia.com
const GENAI = 'genai';  // ai.api.nvidia.com

const _RULES = [
  [['rerank','reranking'], {
    type:'rerank', input:['text'], output:['scores'],
    capabilities:['reranking'],
    endpoints:[{path:'/v1/ranking', host:GENAI, kind:'ranking'}],
    streaming:false }],
  [['embed','embedqa','nv-embed'], {
    type:'embedding', input:['text'], output:['vector'],
    capabilities:['embeddings'],
    endpoints:[{path:'/v1/embeddings', host:LLM, kind:'embeddings'}],
    streaming:false }],
  [['flux','sdxl','stable-diffusion','sd3','sd3.5','stable-diffusion-3',
    'qwen-image','consistory','kandinsky','shuttle'], {
    type:'image', input:['text','image'], output:['image'],
    capabilities:['image_generation','image_to_image'],
    endpoints:[{path:'/v1/images/generations', host:GENAI, kind:'openai_image'},
               {path:'/v1/infer', host:GENAI, kind:'native_infer'}],
    streaming:false }],
  [['parakeet','canary','whisper','asr','conformer','citrinet'], {
    type:'asr', input:['audio'], output:['text'],
    capabilities:['speech_recognition'],
    endpoints:[{path:'/v1/audio/transcriptions', host:GENAI, kind:'asr'}],
    streaming:true }],
  [['magpie-tts','fastpitch','radtts','tts','text-to-speech'], {
    type:'tts', input:['text'], output:['audio'],
    capabilities:['text_to_speech'],
    endpoints:[{path:'/v1/audio/speech', host:GENAI, kind:'tts'}],
    streaming:true }],
  [['fugatto','audiogen','musicgen','audio2','audioldm'], {
    type:'audio', input:['text','audio'], output:['audio'],
    capabilities:['audio_generation','music_generation'],
    endpoints:[{path:'/v1/genai', host:GENAI, kind:'native_infer'}],
    streaming:false }],
  [['cosmos','stable-video','svd','video','ltx','wan2','mochi'], {
    type:'video', input:['text','image'], output:['video'],
    capabilities:['video_generation'],
    endpoints:[{path:'/v1/infer', host:GENAI, kind:'native_infer_async'}],
    streaming:false }],
  [['vila','neva','-vision','vision-','paligemma','kosmos','llava',
    'florence','phi-3-vision','phi-3.5-vision','nvclip'], {
    type:'vision_chat', input:['text','image'], output:['text'],
    capabilities:['chat','vision'],
    endpoints:[{path:'/v1/chat/completions', host:LLM, kind:'chat'}],
    streaming:true }],
  [['ocr','ocdrnet','ocrnet'], {
    type:'ocr', input:['image'], output:['text'],
    capabilities:['ocr'],
    endpoints:[{path:'/v1/infer', host:GENAI, kind:'native_infer'}],
    streaming:false }],
  [['-parse','retriever-parse'], {
    type:'parse', input:['image','document'], output:['text'],
    capabilities:['document_parsing','vision'],
    endpoints:[{path:'/v1/chat/completions', host:LLM, kind:'chat'}],
    streaming:true }],
];

const _CODE_MARKERS = ['code','codestral','starcoder','codegemma','deepseek-coder','qwen-coder'];

const CAPABILITY_PARAMS = {
  chat: {
    required:['model','messages'],
    optional:['temperature','top_p','max_tokens','max_completion_tokens',
      'frequency_penalty','presence_penalty','stop',
      'stream','stream_options','seed',
      'logprobs','top_logprobs','logit_bias',
      'response_format','tools','tool_choice','tool_instances',
      'n','user'],
    nvidia:['top_k','repetition_penalty','length_penalty',
      'min_p','frequency_penalty','presence_penalty',
      'guided_decoding_backend','guided_json','guided_regex',
      'guided_choice','guided_grammar','guided_whitespace_pattern'],
    defaults:{temperature:1.0, top_p:1.0, max_tokens:1024},
  },
  embedding: {
    required:['model','input','input_type'],
    optional:['encoding_format','dimensions','truncate'],
    nvidia:['input_type','truncate'],
    defaults:{input_type:'query', encoding_format:'float'},
  },
  vision_chat: {
    required:['model','messages'],
    optional:['temperature','top_p','max_tokens','max_completion_tokens',
      'frequency_penalty','presence_penalty','stop',
      'stream','stream_options','seed',
      'logprobs','top_logprobs',
      'response_format','tools','tool_choice',
      'n','user','detail'],
    nvidia:['top_k','repetition_penalty','length_penalty',
      'min_p','guided_decoding_backend','guided_json'],
    defaults:{temperature:1.0, top_p:1.0, max_tokens:1024, detail:'auto'},
  },
  parse: {
    required:['model','messages'],
    optional:['temperature','top_p','max_tokens','stream','stream_options','seed'],
    nvidia:['top_k','repetition_penalty'],
    defaults:{temperature:0.0, max_tokens:4096},
  },
  image: {
    required:['model','prompt'],
    optional:['negative_prompt','n','response_format','size','width','height','seed'],
    nvidia:['steps','guidance_scale','strength','num_images','prompt_strength','cfg_scale','sampler',' scheduler'],
    defaults:{width:1024, height:1024, steps:30, guidance_scale:7.5, n:1},
  },
  rerank: {
    required:['model','query','documents'],
    optional:['top_n','return_documents'],
    nvidia:['top_n','return_documents'],
    defaults:{top_n:10, return_documents:true},
  },
  asr: {
    required:['model','file'],
    optional:['language','response_format','temperature','prompt'],
    nvidia:['language','response_format'],
    defaults:{language:'en', response_format:'json'},
  },
  tts: {
    required:['model','input','voice'],
    optional:['response_format','speed'],
    nvidia:['response_format','speed'],
    defaults:{response_format:'mp3', speed:1.0},
  },
  video: {
    required:['model'],
    optional:['prompt','image','seed','duration','fps','width','height'],
    nvidia:['duration','fps','width','height','cfg_scale','steps','seed'],
    defaults:{duration:4, fps:8, width:512, height:512},
  },
  audio: {
    required:['model','prompt'],
    optional:['duration','seed','output_format'],
    nvidia:['duration','output_format','seed'],
    defaults:{duration:10, output_format:'wav'},
  },
  ocr: {
    required:['model','image'],
    optional:['language','output_format'],
    nvidia:['language','bounding_boxes'],
    defaults:{language:'en'},
  },
};

function getCapabilityParams(capType) {
  return CAPABILITY_PARAMS[capType] || CAPABILITY_PARAMS.chat;
}

function getContextWindow(modelId) {
  const mid = (modelId || '').toLowerCase();
  
  // 256k context models
  if (mid.includes('mistral-small-4')) {
    return 262144;
  }

  // 128k context models
  if (
    mid.includes('llama-3.1') || 
    mid.includes('llama-3.3') || 
    mid.includes('llama-3.2') || 
    mid.includes('llama-3-nemotron') || 
    mid.includes('llama-3.3-nemotron') ||
    mid.includes('mistral-large') ||
    mid.includes('mistral-nemo') ||
    (mid.includes('phi-3') && mid.includes('128k')) ||
    mid.includes('qwen2.5') ||
    mid.includes('qwen-2.5') ||
    mid.includes('qwen3') ||
    mid.includes('qwen-3') ||
    mid.includes('deepseek-v3') ||
    mid.includes('deepseek-r1') ||
    mid.includes('deepseek-v4') ||
    mid.includes('gemma-3-12b') ||
    mid.includes('gemma-3-4b') ||
    mid.includes('gemma-3-27b') ||
    mid.includes('gemma-3n-e2b') ||
    mid.includes('gemma-3n-e4b') ||
    mid.includes('gemma-4') ||
    mid.includes('palmyra-creative-122b')
  ) {
    return 131072;
  }
  
  // 32k context models
  if (
    mid.includes('phi-4') ||
    mid.includes('mixtral-8x22b') ||
    mid.includes('yi-large') ||
    mid.includes('nv-embed-v1') ||
    (mid.includes('palmyra') && mid.includes('32k')) ||
    mid.includes('gemma-3-1b')
  ) {
    return 32768;
  }
  
  // 16k context models
  if (
    mid.includes('mistral-7b') ||
    mid.includes('mixtral-8x7b') ||
    mid.includes('deepseek-coder-6.7b')
  ) {
    return 16384;
  }

  // 8k context models
  if (
    mid.includes('llama3-') || 
    mid.includes('llama-3-') || 
    mid.includes('gemma-2') ||
    mid.includes('gemma2') ||
    mid.includes('gemma-2b') ||
    mid.includes('gemma-7b') ||
    mid.includes('kosmos-2') ||
    mid.includes('solar-10.7b') ||
    mid.includes('palmyra-med-70b') ||
    mid.includes('bge-m3')
  ) {
    return 8192;
  }

  // Default 4k context models
  return 4096;
}

function classify(modelId) {
  const mid = (modelId || '').toLowerCase();
  const contextWindow = getContextWindow(modelId);
  
  for (const [keys, desc] of _RULES) {
    if (keys.some(k => mid.includes(k))) {
      const d = JSON.parse(JSON.stringify(desc));
      d.id = modelId;
      d.source = 'heuristic';
      d.supported_params = getCapabilityParams(d.type);
      d.context_window = contextWindow;
      d.context_len = contextWindow;
      d.max_position_embeddings = contextWindow;
      return d;
    }
  }
  const caps = ['chat'];
  if (_CODE_MARKERS.some(k => mid.includes(k))) {
    caps.push('code');
  }
  const result = {
    id: modelId, source: 'heuristic', type: 'chat',
    input: ['text'], output: ['text'], capabilities: caps,
    endpoints: [{ path: '/v1/chat/completions', host: LLM, kind: 'chat' }],
    streaming: true,
    supported_params: getCapabilityParams('chat'),
    context_window: contextWindow,
    context_len: contextWindow,
    max_position_embeddings: contextWindow,
  };
  if (caps.includes('code')) {
    result.capabilities.push('code_generation', 'code_completion');
  }
  return result;
}

const CURATED_GENAI = [
  'nvidia/ai-synthetic-video-detector',
];

const RETIRED_MODELS = {
  'black-forest-labs/flux.1-dev': 'retired_404',
  'black-forest-labs/flux.1-schnell': 'retired_404',
  'black-forest-labs/flux.1-kontext-dev': 'retired_404',
  'stabilityai/stable-diffusion-3.5-large': 'retired_404',
  'stabilityai/stable-diffusion-xl': 'retired_404',
  'nvidia/parakeet-ctc-1.1b-asr': 'retired_404',
  'nvidia/canary-1b-asr': 'retired_404',
  'nvidia/magpie-tts-multilingual': 'retired_404',
  'nvidia/fugatto': 'retired_404',
  'google/gemma-3n-e4b-it': 'retired_404',
  'microsoft/phi-4-mini-instruct': 'retired_404',
  'qwen/qwen3-30b-a3b': 'retired_404',
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
  for (const mid of cachedIds) {
    if (!mid || seen.has(mid)) continue;
    seen.add(mid);
    const d = describe(mid, baseLLM, baseGenai);
    if (mid in RETIRED_MODELS) d.availability = RETIRED_MODELS[mid];
    out.push(d);
  }
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
