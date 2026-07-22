#!/usr/bin/env python3
"""
capabilities.py — Model classification, capability detection, context windows.
Migrated from capabilities.js — functionally identical.

Provides:
  - classify(model_id)           -> model type, streaming, capabilities, params
  - describe(model_id)           -> full descriptor with endpoints
  - build_catalog(ids)           -> full catalog from cached IDs + curated list
  - get_context_window(model_id) -> heuristic context window
  - CAPABILITY_PARAMS            -> supported params per capability type
  - CURATED_GENAI                -> curated non-chat models
  - MODEL_CONTEXT_WINDOWS        -> authoritative context window map
"""

import time
from typing import Dict, List, Optional, Any, Set, Tuple

# ── Constants ────────────────────────────────────────────────────────────
NVIDIA_BASE_URL = 'https://integrate.api.nvidia.com'
NVIDIA_GENAI_URL = 'https://ai.api.nvidia.com'
NVIDIA_NVCF_URL = 'https://api.nvcf.nvidia.com'

DEFAULT_CONTEXT_WINDOW = 131072

# ── Capability type constants ─────────────────────────────────────────────
LLM = 'llm'
GENAI = 'genai'

# ── Curated GenAI models (image/audio/video not in /v1/models) ──────────
CURATED_GENAI = [
    'nvidia/ai-synthetic-video-detector',
    # Image generation — FLUX family
    'black-forest-labs/flux.1-dev',
    'black-forest-labs/flux.1-schnell',
    'black-forest-labs/flux.1-kontext-dev',
    'black-forest-labs/flux.1-canny-dev',
    'black-forest-labs/flux.1-depth-dev',
    'black-forest-labs/flux.2-klein',
    # Image generation — Stability AI
    'stabilityai/stable-diffusion-3.5-large',
    # Image generation — Qwen
    'qwen/qwen-image',
    'qwen/qwen-image-edit',
    # Image generation — other
    'playgroundai/playground-v2.5-1024px-aesthetic',
    'consistory/consistory',
    'kandinsky-community/kandinsky-3',
    # Audio generation
    'nvidia/fugatto',
]

# ── Retired models ──────────────────────────────────────────────────────
RETIRED_MODELS: Dict[str, str] = {}

# ── Model context windows (heuristic, overridden by NGC registry) ──────
# SINGLE SOURCE OF TRUTH for context-window heuristics. Used by both the
# OpenAI path (main.py) and the Anthropic translation path (anthropic_compat.py).
# The NGC registry (registry.py) always wins over these heuristics when it has
# an entry for the model. These are fallbacks only.
MODEL_CONTEXT_WINDOWS: Dict[str, int] = {
    'claude': 200000,
    'gpt-4': 128000,
    'gpt-oss': 128000,
    'llama-3.1': 128000,
    'llama-3.2': 128000,
    'llama-3.3': 128000,
    'llama-3': 128000,
    'llama-4': 131072,
    'llama2': 4096,
    'gemma-3': 128000,
    'gemma-4': 131072,
    'gemma-2': 8192,
    'phi-3.5': 128000,
    'phi-4': 16384,
    'phi-4-mini': 131072,
    'phi-3': 128000,
    # NGC-verified: deepseek-v4-pro context=262144
    'deepseek-v4': 262144,
    'deepseek-coder': 262144,
    'deepseek-r1': 131072,
    'qwen2.5': 128000,
    'qwen3': 131072,
    'qwen3.5': 131072,
    'qwen3-next': 131072,
    'qwen': 32768,
    'kimi': 131072,
    'step': 131072,
    'seed-oss': 131072,
    # NGC-verified: nemotron-3-ultra-550b context=1048576
    'nemotron': 1048576,
    'nemotron-3': 1048576,
    'yi': 1000000,
    'mistral': 131072,
    'mistral-large': 131072,
    'mistral-medium': 131072,
    'mistral-small': 131072,
    'mixtral': 32000,
    'ministral': 131072,
    'codestral': 32768,
    # NGC-verified: glm-5.2 context=202752
    'glm': 202752,
    'minimax': 196608,
    'dbrx': 131072,
    'jamba': 256000,
    'granite': 32768,
    'solar': 32768,
    'kosmos': 8192,
    'dracarys': 131072,
    'zamba': 131072,
}


def get_context_window(model_id: str) -> int:
    """Get heuristic context window for a model."""
    if not model_id:
        return DEFAULT_CONTEXT_WINDOW
    lower = model_id.lower()
    for pattern, window in MODEL_CONTEXT_WINDOWS.items():
        if pattern in lower:
            return window
    return DEFAULT_CONTEXT_WINDOW


# ── Capability definitions ──────────────────────────────────────────────
CAPABILITY_DEFS: Dict[str, dict] = {
    'chat': {
        'type': 'chat',
        'input': ['text'],
        'output': ['text'],
        'capabilities': ['chat', 'completion'],
        'endpoints': [{'path': '/v1/chat/completions', 'host': LLM, 'kind': 'chat'}],
        'streaming': True,
    },
    'vision_chat': {
        'type': 'vision_chat',
        'input': ['text', 'image'],
        'output': ['text'],
        'capabilities': ['chat', 'vision'],
        'endpoints': [{'path': '/v1/chat/completions', 'host': LLM, 'kind': 'chat'}],
        'streaming': True,
    },
    'embedding': {
        'type': 'embedding',
        'input': ['text'],
        'output': ['vector'],
        'capabilities': ['embeddings'],
        'endpoints': [{'path': '/v1/embeddings', 'host': LLM, 'kind': 'embeddings'}],
        'streaming': False,
    },
    'image': {
        'type': 'image',
        'input': ['text', 'image'],
        'output': ['image'],
        'capabilities': ['image_generation', 'image_to_image'],
        'endpoints': [
            {'path': '/v1/images/generations', 'host': GENAI, 'kind': 'openai_image'},
            {'path': '/v1/infer', 'host': GENAI, 'kind': 'native_infer'},
        ],
        'streaming': False,
    },
    'rerank': {
        'type': 'rerank',
        'input': ['text'],
        'output': ['scores'],
        'capabilities': ['reranking'],
        'endpoints': [{'path': '/v1/ranking', 'host': LLM, 'kind': 'ranking'}],
        'streaming': False,
    },
    'asr': {
        'type': 'asr',
        'input': ['audio'],
        'output': ['text'],
        'capabilities': ['speech_recognition'],
        'endpoints': [{'path': '/v1/audio/transcriptions', 'host': GENAI, 'kind': 'asr'}],
        'streaming': True,
    },
    'tts': {
        'type': 'tts',
        'input': ['text'],
        'output': ['audio'],
        'capabilities': ['text_to_speech'],
        'endpoints': [{'path': '/v1/audio/speech', 'host': GENAI, 'kind': 'tts'}],
        'streaming': True,
    },
    'audio': {
        'type': 'audio',
        'input': ['text', 'audio'],
        'output': ['audio'],
        'capabilities': ['audio_generation', 'music_generation'],
        'endpoints': [{'path': '/v1/genai', 'host': GENAI, 'kind': 'native_infer'}],
        'streaming': False,
    },
    'video': {
        'type': 'video',
        'input': ['text', 'image'],
        'output': ['video'],
        'capabilities': ['video_generation'],
        'endpoints': [{'path': '/v1/infer', 'host': GENAI, 'kind': 'native_infer_async'}],
        'streaming': False,
    },
    'ocr': {
        'type': 'ocr',
        'input': ['image'],
        'output': ['text'],
        'capabilities': ['ocr'],
        'endpoints': [{'path': '/v1/infer', 'host': GENAI, 'kind': 'native_infer'}],
        'streaming': False,
    },
    'parse': {
        'type': 'parse',
        'input': ['image', 'document'],
        'output': ['text'],
        'capabilities': ['document_parsing', 'vision'],
        'endpoints': [{'path': '/v1/chat/completions', 'host': LLM, 'kind': 'chat'}],
        'streaming': True,
    },
}

# ── Classification rules (ordered by specificity, most specific first) ──
CLASSIFICATION_RULES: List[Tuple[List[str], str, Optional[List[str]]]] = [
    # Vision/Chat models with vision capabilities
    (['vila', 'neva', '-vision', 'vision-', 'paligemma', 'kosmos', 'llava', 'florence',
      'phi-3-vision', 'phi-3.5-vision', 'phi-4-multimodal', 'nvclip', 'fuyu', 'deplot',
      'pix2struct', 'git-base', 'git-large', 'mm-reasoner', 'qwen2-vl', 'qwen-vl',
      'internvl', 'cogvlm', 'internlm-xcomposer', 'gemma-3', 'llama-3.2-vision',
      'pixtral', 'molmo', 'aria', 'nemotron-3-vision', 'nemotron-vision'], 'vision_chat', None),

    # Code-specific models
    (['code', 'codestral', 'starcoder', 'codegemma', 'deepseek-coder', 'qwen-coder'],
     'chat', ['code_generation', 'code_completion']),

    # Embedding models
    (['embed', 'embedqa', 'nv-embed', 'bge-', 'e5-', 'gte-'], 'embedding', None),

    # Image generation models
    (['flux', 'sdxl', 'stable-diffusion', 'sd3', 'sd3.5', 'stable-diffusion-3', 'qwen-image',
      'consistory', 'kandinsky', 'shuttle', 'playground', 'kolors', 'sana', 'lumina'],
     'image', None),

    # Reranking models
    (['rerank', 'reranking', 'nv-rerank', 'bge-rerank'], 'rerank', None),

    # ASR (Speech Recognition) models
    (['parakeet', 'canary', 'whisper', 'asr', 'conformer', 'citrinet', 'nemo-asr'], 'asr', None),

    # TTS (Text-to-Speech) models
    (['magpie-tts', 'fastpitch', 'radtts', 'tts', 'text-to-speech', 'xtts', 'bark',
      'valle', 'nemo-tts'], 'tts', None),

    # Audio generation models
    (['fugatto', 'audiogen', 'musicgen', 'audio2', 'audioldm', 'musiclm', 'audiocraft',
      'encodec'], 'audio', None),

    # Curated override — BEFORE the generic video rule below. nvidia/cosmos-reason2-8b
    # contains "cosmos" (which the video rule would catch) but is actually a REASONING
    # LLM (text-in/text-out) served on /v1/chat/completions, NOT a video-diffuser.
    (['cosmos-reason', 'cosmos-reason2'], 'chat', ['reasoning']),

    # Video generation models
    (['cosmos', 'stable-video', 'svd', 'video', 'ltx', 'wan2', 'mochi', 'videocrafter',
      'modelscope', 'videopoet', 'phenaki', 'make-a-video'], 'video', None),

    # OCR models
    (['ocr', 'ocdrnet', 'ocrnet', 'paddleocr', 'trocr'], 'ocr', None),

    # Document parsing models
    (['-parse', 'retriever-parse', 'layoutlm', 'donut', 'nougat'], 'parse', None),

    # Default: chat model
    ([], 'chat', None),
]

# ── Capability params per type ──────────────────────────────────────────
CAPABILITY_PARAMS: Dict[str, dict] = {
    'chat': {
        'required': ['model', 'messages'],
        'optional': ['temperature', 'top_p', 'max_tokens', 'max_completion_tokens',
                     'frequency_penalty', 'presence_penalty', 'stop',
                     'stream', 'stream_options', 'seed',
                     'logprobs', 'top_logprobs', 'logit_bias',
                     'response_format', 'tools', 'tool_choice', 'tool_instances',
                     'n', 'user'],
        'nvidia': ['top_k', 'repetition_penalty', 'length_penalty',
                   'min_p', 'frequency_penalty', 'presence_penalty',
                   'guided_decoding_backend', 'guided_json', 'guided_regex',
                   'guided_choice', 'guided_grammar', 'guided_whitespace_pattern'],
        'defaults': {'temperature': 1.0, 'top_p': 1.0, 'max_tokens': 1024},
    },
    'embedding': {
        'required': ['model', 'input', 'input_type'],
        'optional': ['encoding_format', 'dimensions', 'truncate'],
        'nvidia': ['input_type', 'truncate'],
        'defaults': {'input_type': 'query', 'encoding_format': 'float'},
    },
    'vision_chat': {
        'required': ['model', 'messages'],
        'optional': ['temperature', 'top_p', 'max_tokens', 'max_completion_tokens',
                     'frequency_penalty', 'presence_penalty', 'stop',
                     'stream', 'stream_options', 'seed',
                     'logprobs', 'top_logprobs',
                     'response_format', 'tools', 'tool_choice',
                     'n', 'user', 'detail'],
        'nvidia': ['top_k', 'repetition_penalty', 'length_penalty',
                   'min_p', 'guided_decoding_backend', 'guided_json'],
        'defaults': {'temperature': 1.0, 'top_p': 1.0, 'max_tokens': 1024, 'detail': 'auto'},
    },
    'parse': {
        'required': ['model', 'messages'],
        'optional': ['temperature', 'top_p', 'max_tokens', 'stream', 'stream_options', 'seed'],
        'nvidia': ['top_k', 'repetition_penalty'],
        'defaults': {'temperature': 0.0, 'max_tokens': 4096},
    },
    'image': {
        'required': ['model', 'prompt'],
        'optional': ['negative_prompt', 'n', 'response_format', 'size', 'width', 'height', 'seed'],
        'nvidia': ['steps', 'guidance_scale', 'strength', 'num_images', 'prompt_strength',
                   'cfg_scale', 'sampler', 'scheduler'],
        'defaults': {'width': 1024, 'height': 1024, 'steps': 30, 'guidance_scale': 7.5, 'n': 1},
    },
    'rerank': {
        'required': ['model', 'query', 'documents'],
        'optional': ['top_n', 'return_documents'],
        'nvidia': ['top_n', 'return_documents'],
        'defaults': {'top_n': 10, 'return_documents': True},
    },
    'asr': {
        'required': ['model', 'file'],
        'optional': ['language', 'response_format', 'temperature', 'prompt'],
        'nvidia': ['language', 'response_format'],
        'defaults': {'language': 'en', 'response_format': 'json'},
    },
    'tts': {
        'required': ['model', 'input', 'voice'],
        'optional': ['response_format', 'speed'],
        'nvidia': ['response_format', 'speed'],
        'defaults': {'response_format': 'mp3', 'speed': 1.0},
    },
    'video': {
        'required': ['model'],
        'optional': ['prompt', 'image', 'seed', 'duration', 'fps', 'width', 'height'],
        'nvidia': ['duration', 'fps', 'width', 'height', 'cfg_scale', 'steps', 'seed'],
        'defaults': {'duration': 4, 'fps': 8, 'width': 512, 'height': 512},
    },
    'audio': {
        'required': ['model', 'prompt'],
        'optional': ['duration', 'seed', 'output_format'],
        'nvidia': ['duration', 'output_format', 'seed'],
        'defaults': {'duration': 10, 'output_format': 'wav'},
    },
    'ocr': {
        'required': ['model', 'image'],
        'optional': ['language', 'output_format'],
        'nvidia': ['language', 'bounding_boxes'],
        'defaults': {'language': 'en'},
    },
}


def get_capability_params(cap_type: str) -> dict:
    """Get supported params for a capability type."""
    return CAPABILITY_PARAMS.get(cap_type, CAPABILITY_PARAMS.get('chat', {}))


# ── Classify cache (TTL-based eviction) ─────────────────────────────────
_classify_cache: Dict[str, Tuple[dict, float]] = {}
_CLASSIFY_CACHE_MAX = 500
_CLASSIFY_CACHE_TTL_MS = 3600 * 1000  # 1 hour


def _clone_result(r: dict) -> dict:
    """Deep-clone a cached classify result so callers cannot corrupt the cache."""
    result = dict(r)
    if 'input' in result:
        result['input'] = list(result['input'])
    if 'output' in result:
        result['output'] = list(result['output'])
    if 'capabilities' in result:
        result['capabilities'] = list(result['capabilities'])
    if 'endpoints' in result:
        result['endpoints'] = [dict(ep) for ep in result['endpoints']]
    if 'supported_params' in result:
        result['supported_params'] = dict(result['supported_params']) if isinstance(result['supported_params'], dict) else set(result['supported_params'])
    return result


def classify(model_id: str) -> dict:
    """Classify a model by its ID. Returns type, streaming, capabilities, etc."""
    if not model_id:
        return {
            'id': '', 'source': 'heuristic',
            'type': 'chat', 'input': ['text'], 'output': ['text'],
            'capabilities': ['chat', 'completion'],
            'endpoints': [{'path': '/v1/chat/completions', 'host': LLM, 'kind': 'chat'}],
            'streaming': True,
            'supported_params': dict(CAPABILITY_PARAMS['chat']),
        }

    now_ms = time.time() * 1000
    cached = _classify_cache.get(model_id)
    if cached:
        if now_ms - cached[1] < _CLASSIFY_CACHE_TTL_MS:
            return _clone_result(cached[0])
        del _classify_cache[model_id]

    mid = model_id.lower()

    for patterns, mtype, extra_caps in CLASSIFICATION_RULES:
        if not patterns or any(p in mid for p in patterns):
            base_def = CAPABILITY_DEFS.get(mtype, CAPABILITY_DEFS['chat'])
            result = {
                'id': model_id,
                'source': 'heuristic',
                'type': base_def['type'],
                'input': list(base_def['input']),
                'output': list(base_def['output']),
                'capabilities': list(base_def['capabilities']),
                'endpoints': [dict(ep) for ep in base_def['endpoints']],
                'streaming': base_def['streaming'],
                'supported_params': dict(get_capability_params(mtype)),
            }

            if extra_caps:
                result['capabilities'] = list(dict.fromkeys(result['capabilities'] + extra_caps))

            # Add code capabilities if model name indicates code
            if any(kw in mid for kw in ['code', 'coder', 'codestral', 'starcoder']):
                result['capabilities'] = list(dict.fromkeys(result['capabilities'] + ['code_generation', 'code_completion']))

            # Evict oldest entry if cache exceeds limit
            if len(_classify_cache) >= _CLASSIFY_CACHE_MAX:
                oldest_key = next(iter(_classify_cache))
                del _classify_cache[oldest_key]

            result['_cached_at'] = time.time()
            _classify_cache[model_id] = (result, now_ms)
            return _clone_result(result)

    # Fallback (unreachable because last rule has patterns=[])
    base_def = CAPABILITY_DEFS['chat']
    result = {
        'id': model_id, 'source': 'heuristic',
        'type': base_def['type'],
        'input': list(base_def['input']),
        'output': list(base_def['output']),
        'capabilities': list(base_def['capabilities']),
        'endpoints': [dict(ep) for ep in base_def['endpoints']],
        'streaming': base_def['streaming'],
        'supported_params': dict(get_capability_params('chat')),
    }
    result['_cached_at'] = time.time()
    _classify_cache[model_id] = (result, now_ms)
    return _clone_result(result)


def _resolve_hosts(desc: dict, base_llm: str, base_genai: str) -> dict:
    """Resolve endpoint base_url from host labels."""
    hosts = {LLM: base_llm.rstrip('/'), GENAI: base_genai.rstrip('/')}
    for ep in (desc.get('endpoints') or []):
        ep['base_url'] = hosts.get(ep['host'], base_llm.rstrip('/'))
    return desc


def describe(model_id: str, base_llm: str = NVIDIA_BASE_URL,
             base_genai: str = NVIDIA_GENAI_URL) -> dict:
    """Full model descriptor with endpoints."""
    return _resolve_hosts(classify(model_id), base_llm, base_genai)


def build_catalog(model_ids: List[str], base_llm: str = NVIDIA_BASE_URL,
                  base_genai: str = NVIDIA_GENAI_URL) -> List[dict]:
    """Build full catalog from cached model IDs + curated GenAI models."""
    seen = set()
    out = []

    for mid in model_ids:
        if not mid or mid in seen:
            continue
        seen.add(mid)
        d = describe(mid, base_llm, base_genai)
        if mid in RETIRED_MODELS:
            d['availability'] = RETIRED_MODELS[mid]
        out.append(d)

    for mid in CURATED_GENAI:
        if mid in seen:
            continue
        seen.add(mid)
        d = describe(mid, base_llm, base_genai)
        d['source'] = 'curated'
        if mid in RETIRED_MODELS:
            d['availability'] = RETIRED_MODELS[mid]
        out.append(d)

    return out


def summarize(catalog: List[dict]) -> dict:
    """Summarize catalog by type."""
    by_type: Dict[str, int] = {}
    for d in catalog:
        t = d.get('type', 'unknown')
        by_type[t] = by_type.get(t, 0) + 1
    return {'total': len(catalog), 'by_type': by_type}
