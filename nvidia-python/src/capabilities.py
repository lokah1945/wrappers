#!/usr/bin/env python3
"""
capabilities.py — Model classification, capability detection, context windows.
Migrated from capabilities.js — functionally identical.

Provides:
  - classify(model_id)           -> model type, streaming, capabilities, params
  - describe(model_id)           -> full descriptor with endpoints
  - build_catalog(ids)           -> full catalog from cached IDs + curated list
  - get_context_window(model_id) -> unknown without provider metadata
  - CAPABILITY_PARAMS            -> supported params per capability type
  - CURATED_GENAI                -> curated non-chat models
  - MODEL_CONTEXT_WINDOWS        -> empty compatibility symbol
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from common.nvidia_build_catalog import (
    BUILD_CATALOG_RETRIEVED,
    BUILD_CATALOG_SOURCE,
    BUILD_CONTRACTS,
    BUILD_MODEL_IDS,
    get_build_contract,
)

# ── Constants ────────────────────────────────────────────────────────────
NVIDIA_BASE_URL = 'https://integrate.api.nvidia.com'
NVIDIA_GENAI_URL = 'https://ai.api.nvidia.com'
NVIDIA_NVCF_URL = 'https://api.nvcf.nvidia.com'

DEFAULT_CONTEXT_WINDOW = None

# ── Capability type constants ─────────────────────────────────────────────
LLM = 'llm'
GENAI = 'genai'
NVCF = 'nvcf'
NVIDIA_MAXINE_GRPC_URL = 'grpc://grpc.nvcf.nvidia.com:443'

# ── Curated GenAI models (image/audio/video not in /v1/models) ──────────
CURATED_GENAI = [
    'nvidia/ai-synthetic-video-detector',
    # Image generation — FLUX family
    'black-forest-labs/flux.1-dev',
    'black-forest-labs/flux.1-schnell',
    'black-forest-labs/flux.1-kontext-dev',
    'black-forest-labs/flux.1-canny-dev',
    'black-forest-labs/flux.1-depth-dev',
    # Build lists this model as Downloadable, but its NVIDIA GenAI endpoint
    # is separately verified and is not present in the LLM /v1/models sweep.
    'black-forest-labs/flux-3-image-klein',
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

# Provenance for Build entries that are not part of the LLM model sweep.
# This metadata intentionally does not claim runtime availability; the
# account-scoped verifier/live response state remains authoritative.
CURATED_CATALOG_METADATA: Dict[str, dict] = {
    'black-forest-labs/flux-3-image-klein': {
        'source': 'nvidia_build.com',
        'build_access': 'downloadable',
        'provider_catalog_presence': 'not_listed_at_reconciliation',
        'runtime_contract': 'explicit_native_genai_endpoint',
        'runtime_endpoint': 'https://ai.api.nvidia.com/v1/genai/{model}',
    },
}

# ── Retired models ──────────────────────────────────────────────────────
RETIRED_MODELS: Dict[str, str] = {}

# Context limits belong to exact upstream model records (registry.py).
# Retain these symbols for callers, but unknown must never become 131072.
MODEL_CONTEXT_WINDOWS: Dict[str, int] = {}


def get_context_window(model_id: str) -> Optional[int]:
    """No context capacity can be inferred safely from a model ID alone."""
    return None


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
    'video_analysis': {
        'type': 'video_analysis',
        'input': ['video'],
        'output': ['classification'],
        'capabilities': ['video_analysis', 'grpc', 'nvidia_maxine'],
        'endpoints': [{
            'path': NVIDIA_MAXINE_GRPC_URL,
            'host': NVCF,
            'kind': 'grpc',
            'protocol': 'grpc',
            'client': 'NVIDIA Maxine generated protobuf client',
        }],
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
    'structured': {
        'type': 'structured',
        'input': ['tabular', 'text'],
        'output': ['json', 'scores'],
        'capabilities': ['structured_prediction', 'tabular'],
        'endpoints': [{'path': '/v1/native/{model}', 'host': GENAI, 'kind': 'native_structured'}],
        'streaming': False,
    },
    'climate': {
        'type': 'climate',
        'input': ['array', 'weather'],
        'output': ['array', 'forecast'],
        'capabilities': ['weather_forecasting', 'async_inference'],
        'endpoints': [{'path': '/v1/native/{model}', 'host': NVCF, 'kind': 'native_climate'}],
        'streaming': False,
    },
}

# ── Classification rules (ordered by specificity, most specific first) ──
CLASSIFICATION_RULES: List[Tuple[List[str], str, Optional[List[str]]]] = [
    # This hosted service is video analysis over NVIDIA Maxine gRPC, not a
    # REST video-generation or generic chat model.
    (['ai-synthetic-video-detector'], 'video_analysis', None),

    # Llama Guard 4 is a multimodal chat-completions classifier. Its model
    # card requires alternating user/assistant messages; keep the vision
    # capability visible instead of treating it as generic text-only chat.
    (['llama-guard-4-12b'], 'vision_chat', None),

    # Vision/Chat models with vision capabilities
    (['vila', 'neva', '-vision', 'vision-', 'paligemma', 'kosmos', 'llava', 'florence',
      'phi-3-vision', 'phi-3.5-vision', 'phi-4-multimodal', 'fuyu', 'deplot',
      'pix2struct', 'git-base', 'git-large', 'mm-reasoner', 'qwen2-vl', 'qwen-vl',
      'internvl', 'cogvlm', 'internlm-xcomposer', 'gemma-3', 'llama-3.2-vision',
      'pixtral', 'molmo', 'aria', 'nemotron-3-vision', 'nemotron-vision'], 'vision_chat', None),

    # Code-specific models
    (['code', 'codestral', 'starcoder', 'codegemma', 'deepseek-coder', 'qwen-coder'],
     'chat', ['code_generation', 'code_completion']),

    # Embedding models
    (['embed', 'embedqa', 'nv-embed', 'nvclip', 'bge-', 'e5-', 'gte-'], 'embedding', None),

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
    'video_analysis': {
        'required': ['model', 'video'],
        'optional': ['threshold', 'output_format'],
        'nvidia': ['protobuf_method', 'video_codec'],
        'defaults': {'output_format': 'json'},
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
    """Resolve exact wrapper/upstream route metadata from host labels.

    Image generation is model-suffixed upstream even though the client calls
    ``/v1/images/generations``; expose that as a URL template instead of
    forcing clients to reverse-engineer the handler.
    """
    hosts = {LLM: base_llm.rstrip('/'), GENAI: base_genai.rstrip('/')}
    for ep in (desc.get('endpoints') or []):
        path = str(ep.get('path') or '')
        if ep.get('protocol') == 'grpc' or ep.get('kind') == 'grpc':
            # There is no HTTP path to synthesize for a generated-protobuf
            # service. Preserve the native gRPC target and tell clients which
            # protocol/client they need instead of advertising a false REST URL.
            target = str(ep.get('url') or path)
            ep['base_url'] = target
            ep['client_path'] = None
            ep['upstream_path'] = ''
            ep['url'] = target
            ep['route_source'] = 'provider_contract'
            ep['route_confidence'] = 'exact'
            ep['model_field'] = None
            continue
        base = hosts.get(ep['host'], base_llm.rstrip('/'))
        ep['base_url'] = base
        ep['client_path'] = path
        ep['upstream_path'] = path
        ep['route_source'] = 'wrapper_contract'
        ep['route_confidence'] = 'exact'
        ep['model_field'] = 'model'
        if ep.get('kind') == 'openai_image':
            ep['upstream_path_template'] = '/v1/genai/{model}'
            ep['url_template'] = f'{base}/v1/genai/{{model}}'
        else:
            ep['url'] = f'{base}{path}'
    return desc


def _join_contract_url(host: str | None, path: str | None) -> str | None:
    """Join a documented server and path without changing either contract."""
    if not host:
        return None
    host = str(host).rstrip('/')
    path = str(path or '')
    if not path.startswith('/'):
        path = '/' + path
    # ReadMe/OpenAPI specs sometimes put /v1 in the server and sometimes in
    # the operation path.  Preserve the operation path and remove only the
    # duplicate separator/prefix produced by that documentation convention.
    if host.endswith('/v1') and path.startswith('/v1/'):
        path = path[3:]
    return host + path


def _build_contract_descriptor(model_id: str, base_llm: str,
                               base_genai: str) -> dict | None:
    row = get_build_contract(model_id)
    if not row:
        return None
    contract = row.get('contract') or {}
    kind = str(contract.get('kind') or '')
    documented_host = contract.get('host')
    if isinstance(documented_host, str) and documented_host.startswith('http'):
        url = _join_contract_url(documented_host, contract.get('path'))
    elif contract.get('host_class') == 'llm':
        url = _join_contract_url(base_llm, contract.get('path'))
    elif contract.get('host_class') == 'genai':
        url = _join_contract_url(base_genai, contract.get('path'))
    elif contract.get('host_class') == 'ai':
        url = _join_contract_url(base_genai, contract.get('path'))
    elif contract.get('host_class') == 'health':
        url = _join_contract_url('https://health.api.nvidia.com/v1', contract.get('path'))
    elif contract.get('nvcf_function_id'):
        url = _join_contract_url(
            'https://' + str(contract.get('nvcf_function_id')) + '.invocation.api.nvcf.nvidia.com',
            contract.get('path'))
    elif contract.get('host_class') == 'climate':
        url = _join_contract_url('https://climate.api.nvidia.com/v1', contract.get('path'))
    elif contract.get('host_class') == 'optimize':
        url = _join_contract_url('https://optimize.api.nvidia.com/v1', contract.get('path'))
    else:
        url = None

    path = str(contract.get('path') or '')
    lower_path = path.lower()
    if kind in ('grpc',):
        mtype = 'video_analysis'
        client_path = '/v1/native/{model}'
        endpoint_kind = 'grpc'
    elif kind == 'riva':
        if '/speech' in lower_path or 'tts' in model_id.lower() or 'chatterbox' in model_id.lower() or 'magpie' in model_id.lower():
            mtype = 'tts'
        elif 'translation' in lower_path or 'translate' in model_id.lower() or 'megatron' in model_id.lower():
            mtype = 'chat'
        else:
            mtype = 'asr'
        client_path = path
        endpoint_kind = 'native_audio'
    elif kind == 'visual_nim':
        mtype = 'video' if any(x in model_id.lower() for x in ('wan2', 'cosmos', 'video')) else 'image'
        client_path = '/v1/native/{model}'
        endpoint_kind = 'native_infer'
    elif kind == 'native_multipart':
        mtype = 'video'
        client_path = '/v1/native/{model}'
        endpoint_kind = 'native_multipart'
    elif 'embedding' in lower_path:
        mtype = 'embedding'; client_path = '/v1/embeddings'; endpoint_kind = 'embeddings'
    elif 'retrieval' in lower_path or 'rerank' in model_id.lower():
        mtype = 'rerank'; client_path = '/v1/ranking'; endpoint_kind = 'ranking'
    elif 'structured-data/' in lower_path:
        mtype = 'structured'; client_path = '/v1/native/{model}'; endpoint_kind = 'native_structured'
    elif 'climate.api' in str(documented_host).lower() or 'fourcastnet' in model_id.lower():
        mtype = 'climate'; client_path = '/v1/native/{model}'; endpoint_kind = 'native_climate'
    elif '/cosmos/' in lower_path:
        mtype = 'video'; client_path = '/v1/native/{model}'; endpoint_kind = 'native_cosmos'
    elif 'genai/' in lower_path:
        mtype = 'image' if any(x in model_id.lower() for x in ('flux', 'image', 'trellis', 'stable-diffusion')) else 'video'
        client_path = '/v1/images/generations' if mtype == 'image' else '/v1/native/{model}'
        endpoint_kind = 'native_genai'
    elif '/chat/completions' in lower_path:
        # A path-qualified vision model is still a chat contract, but it must
        # not be advertised as the generic route.
        mtype = 'vision_chat' if any(x in model_id.lower() for x in ('vision', 'vl-', 'super-vl', 'paligemma', 'diffusiongemma', 'parse')) else 'chat'
        client_path = '/v1/chat/completions'; endpoint_kind = 'chat'
    elif '/embeddings' in lower_path:
        mtype = 'embedding'; client_path = '/v1/embeddings'; endpoint_kind = 'embeddings'
    elif '/cv/' in lower_path:
        mtype = 'ocr'; client_path = '/v1/native/{model}'; endpoint_kind = 'native_cv'
    elif '/biology/' in lower_path or '/molecular-docking/' in lower_path:
        mtype = 'chat'; client_path = '/v1/native/{model}'; endpoint_kind = 'native_biology'
    elif '/security/' in lower_path:
        mtype = 'chat'; client_path = '/v1/native/{model}'; endpoint_kind = 'native_security'
    else:
        mtype = 'chat'; client_path = '/v1/native/{model}'; endpoint_kind = 'native_nim'

    endpoint = {
        'path': client_path,
        'host': 'NVCF' if kind == 'grpc' else ('LLM' if (url or '').startswith(base_llm.rstrip('/')) else 'GENAI'),
        'kind': endpoint_kind,
        'client_path': client_path,
        'upstream_path': path,
        'route_source': 'nvidia_build_contract',
        'route_confidence': 'exact' if contract.get('verification') in ('hosted_contract', 'native_protocol') else 'documented',
        'model_field': 'model' if mtype in ('chat', 'embedding', 'rerank') else None,
        'nvcf_function_id': contract.get('nvcf_function_id'),
    }
    if url:
        endpoint['url'] = url
    if kind == 'grpc':
        endpoint['url'] = 'grpc://grpc.nvcf.nvidia.com:443'
        endpoint['protocol'] = 'grpc'
        endpoint['client'] = 'NVIDIA native gRPC client'
        endpoint['grpc_method'] = contract.get('grpc_method')
    if '{model}' in client_path:
        endpoint['url_template'] = url or contract.get('path')
    if 'genai/' in lower_path:
        # Compatibility metadata for clients that understand the documented
        # NVIDIA GenAI family template; ``url``/``upstream_path`` retain the
        # exact model operation (including punctuation such as flux.3).
        endpoint.setdefault('url_template', 'https://ai.api.nvidia.com/v1/genai/{model}')
    return {
        'id': model_id,
        'source': 'nvidia_build.com',
        'type': mtype,
        'input': list(CAPABILITY_DEFS.get(mtype, CAPABILITY_DEFS['chat'])['input']),
        'output': list(CAPABILITY_DEFS.get(mtype, CAPABILITY_DEFS['chat'])['output']),
        'capabilities': list(CAPABILITY_DEFS.get(mtype, CAPABILITY_DEFS['chat'])['capabilities']),
        'endpoints': [endpoint],
        'streaming': bool(CAPABILITY_DEFS.get(mtype, CAPABILITY_DEFS['chat'])['streaming']),
        'supported_params': dict(get_capability_params(mtype)),
        'build_metadata': {
            'build_url': row.get('build_url'),
            'title': row.get('title'),
            'badges': list(row.get('badges') or []),
            'downloadable': bool(row.get('downloadable')),
            'free_endpoint': bool(row.get('free_endpoint')),
            'deprecated': bool(row.get('deprecated')),
            'hosted_function': bool(row.get('hosted_function')),
            'api_reference': row.get('api_reference'),
            'contract_kind': kind,
            'hosted_path': contract.get('path'),
            'deployment_path': contract.get('deployment_path'),
            'contract_verification': contract.get('verification', 'unknown'),
            'contract_source': row.get('api_reference') or BUILD_CATALOG_SOURCE,
            'catalog_retrieved': BUILD_CATALOG_RETRIEVED,
        },
    }


def describe(model_id: str, base_llm: str = NVIDIA_BASE_URL,
             base_genai: str = NVIDIA_GENAI_URL) -> dict:
    """Full model descriptor with the exact Build/NIM route when known."""
    build_desc = _build_contract_descriptor(model_id, base_llm, base_genai)
    result = build_desc or _resolve_hosts(classify(model_id), base_llm, base_genai)
    metadata = CURATED_CATALOG_METADATA.get(model_id)
    if metadata:
        result['catalog_metadata'] = dict(metadata)
    elif model_id == 'black-forest-labs/flux-3-image-klein':
        # Keep the historical provenance shape while the richer Build contract
        # is exposed alongside it. This is metadata, not an availability claim.
        result['catalog_metadata'] = {
            'source': 'nvidia_build.com',
            'build_access': 'downloadable',
            'provider_catalog_presence': 'not_listed_at_reconciliation',
            'runtime_contract': 'explicit_native_genai_endpoint',
            'runtime_endpoint': 'https://ai.api.nvidia.com/v1/genai/{model}',
        }
    return result


def build_catalog(model_ids: List[str], base_llm: str = NVIDIA_BASE_URL,
                  base_genai: str = NVIDIA_GENAI_URL) -> List[dict]:
    """Build the reconciled NVIDIA catalog.

    Build is authoritative for the current NIM surface.  The hosted provider
    list remains an observation used by the server to annotate each Build row;
    it is not allowed to delete a Downloadable/native entry.  The small
    fallback keeps isolated unit callers that pass an ad-hoc list compatible.
    """
    supplied = [str(x) for x in (model_ids or []) if x]
    use_build = bool(BUILD_MODEL_IDS) and (
        len(supplied) >= 20 or any(x in BUILD_CONTRACTS for x in supplied)
    )
    ids = list(BUILD_MODEL_IDS) if use_build else supplied
    if not use_build:
        ids.extend(x for x in CURATED_GENAI if x not in ids)
    seen = set()
    out = []
    for mid in ids:
        if not mid or mid in seen:
            continue
        seen.add(mid)
        d = describe(mid, base_llm, base_genai)
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
