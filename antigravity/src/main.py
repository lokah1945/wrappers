import os
import sys
import uuid
import time
import asyncio
import logging
import random
import httpx
from typing import List, Dict, Any, Optional, Union
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from logging.handlers import RotatingFileHandler

# Add local path to import utils
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import utils

# Configure Enterprise-Grade Logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("antigravity-wrapper")
logger.setLevel(logging.INFO)

# File handler for log persistence (Rotating, max 10MB, up to 5 backups)
log_file_path = "/root/wrapper/antigravity/server.log"
try:
    file_handler = RotatingFileHandler(
        log_file_path,
        maxBytes=10*1024*1024,
        backupCount=5
    )
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
except Exception as e:
    logger.warning(f"Could not initialize file log handler: {e}")

app = FastAPI(
    title="Antigravity API Wrapper",
    description="Enterprise-grade and highly resilient OpenAI-compatible API Wrapper for Antigravity & NVIDIA NIM"
)

# Concurrency Semaphore to prevent system/CPU thrashing by agy CLI executions
MAX_CONCURRENT_AGY = 5
agy_semaphore = asyncio.Semaphore(MAX_CONCURRENT_AGY)

# Model Mapping for robust client support (exact agy model names, clean dash names, and common aliases)
MODEL_MAPPING = {
    # Exact names provided by agy models
    "Gemini 3.5 Flash (Medium)": "Gemini 3.5 Flash (Medium)",
    "Gemini 3.5 Flash (High)": "Gemini 3.5 Flash (High)",
    "Gemini 3.5 Flash (Low)": "Gemini 3.5 Flash (Low)",
    "Gemini 3.1 Pro (Low)": "Gemini 3.1 Pro (Low)",
    "Gemini 3.1 Pro (High)": "Gemini 3.1 Pro (High)",
    "Claude Sonnet 4.6 (Thinking)": "Claude Sonnet 4.6 (Thinking)",
    "Claude Opus 4.6 (Thinking)": "Claude Opus 4.6 (Thinking)",
    "GPT-OSS 120B (Medium)": "GPT-OSS 120B (Medium)",

    # Cleaned lowercase names with dashes
    "gemini-3.5-flash-medium": "Gemini 3.5 Flash (Medium)",
    "gemini-3.5-flash-high": "Gemini 3.5 Flash (High)",
    "gemini-3.5-flash-low": "Gemini 3.5 Flash (Low)",
    "gemini-3.1-pro-low": "Gemini 3.1 Pro (Low)",
    "gemini-3.1-pro-high": "Gemini 3.1 Pro (High)",
    "claude-sonnet-4.6-thinking": "Claude Sonnet 4.6 (Thinking)",
    "claude-opus-4.6-thinking": "Claude Opus 4.6 (Thinking)",
    "gpt-oss-120b-medium": "GPT-OSS 120B (Medium)",

    # Common standard aliases
    "gemini-3.5-flash": "Gemini 3.5 Flash (High)",
    "gemini-3.1-pro": "Gemini 3.1 Pro (High)",
    "claude-3-5-sonnet": "Claude Sonnet 4.6 (Thinking)",
    "claude-3-opus": "Claude Opus 4.6 (Thinking)",
    "claude-sonnet": "Claude Sonnet 4.6 (Thinking)",
    "claude-opus": "Claude Opus 4.6 (Thinking)",
    "gpt-4": "Claude Sonnet 4.6 (Thinking)",
    "gpt-4o": "Gemini 3.5 Flash (High)",
    "antigravity": "Gemini 3.5 Flash (High)",
    "default": "Gemini 3.5 Flash (High)"
}

# Model Schemas - Flexible and accepting extra parameters to prevent 422 errors
class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[Any], None] = None

    model_config = {
        "extra": "allow"
    }

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    user: Optional[str] = None

    model_config = {
        "extra": "allow"
    }

# Local Cache of available AGY models (populated dynamically)
AGY_MODELS_CACHE = []

async def fetch_agy_models_dynamically():
    """
    Executes 'agy models' to discover available Google/Antigravity models dynamically.
    """
    global AGY_MODELS_CACHE
    try:
        process = await asyncio.create_subprocess_exec(
            "agy", "models",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            lines = stdout.decode("utf-8", errors="replace").splitlines()
            models = []
            for line in lines:
                line_str = line.strip()
                if not line_str or "fetching" in line_str.lower() or "..." in line_str:
                    continue
                models.append(line_str)
            if models:
                AGY_MODELS_CACHE = models
                logger.info(f"Dynamically populated {len(models)} models from agy models CLI.")
                return
    except Exception as e:
        logger.warning(f"Could not connect to agy CLI to load models: {e}. Using pre-defined list.")
    
    # Fallback if command fails
    AGY_MODELS_CACHE = [
        "Gemini 3.5 Flash (Medium)",
        "Gemini 3.5 Flash (High)",
        "Gemini 3.5 Flash (Low)",
        "Gemini 3.1 Pro (Low)",
        "Gemini 3.1 Pro (High)",
        "Claude Sonnet 4.6 (Thinking)",
        "Claude Opus 4.6 (Thinking)",
        "GPT-OSS 120B (Medium)"
    ]

@app.on_event("startup")
async def startup_event():
    await fetch_agy_models_dynamically()

def classify_agy_model(model_id: str) -> Dict[str, Any]:
    """
    Enriches model metadata with capabilities, context windows, and parameters.
    Adapted from the wrapper-nvidia capabilities classification concept.
    """
    mid = model_id.lower()
    
    owned_by = "google"
    capabilities = ["chat"]
    context_window = 1048576  # Default 1M context for Gemini
    
    if "claude" in mid:
        owned_by = "anthropic"
        context_window = 200000
        capabilities.extend(["vision", "thinking", "code_generation"])
    elif "gpt" in mid:
        owned_by = "oss"
        context_window = 32768
        capabilities.extend(["code_generation"])
    else:
        capabilities.extend(["vision", "code_generation"])
        
    supported_params = {
        "required": ["model", "messages"],
        "optional": [
            "temperature", "top_p", "max_tokens", "max_completion_tokens",
            "frequency_penalty", "presence_penalty", "stop", "stream",
            "stream_options", "seed", "user"
        ],
        "defaults": {
            "temperature": 0.7,
            "max_tokens": 4096
        }
    }
    
    return {
        "id": model_id,
        "object": "model",
        "created": 1782668134,
        "owned_by": owned_by,
        "context_window": context_window,
        "capabilities": capabilities,
        "supported_params": supported_params
    }

def resolve_agy_model(model_name: str) -> str:
    """
    Resolves client request model name to the exact native AGY CLI model name.
    """
    mapped = MODEL_MAPPING.get(model_name)
    if mapped:
        return mapped
        
    for k, v in MODEL_MAPPING.items():
        if k.lower() == model_name.lower():
            return v
            
    for m in AGY_MODELS_CACHE:
        if m.lower() == model_name.lower():
            return m
            
    return "Gemini 3.5 Flash (High)"

def clean_agy_output(text: str) -> str:
    """
    Cleans internal agy startup warnings and noise from the final output.
    """
    lines = text.splitlines()
    cleaned = [l for l in lines if not (l.strip().startswith("Warning: conversation") and "not found" in l)]
    res = "\n".join(cleaned)
    if text.endswith("\n") and not res.endswith("\n") and len(res) > 0:
        res += "\n"
    return res

def format_openai_response(content: str, model: str) -> Dict[str, Any]:
    """
    Formats the string response into an OpenAI-compatible JSON structure.
    """
    return {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": clean_agy_output(content)
                },
                "finish_reason": "stop"
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0
        }
    }

def import_json_dump(data: Any) -> str:
    import json
    return json.dumps(data)

# SUBPROCESS RUNNER FOR AGY (Resilient & Stateful)
# Enterprise Exception for subprocess execution errors
class AgySubprocessError(Exception):
    def __init__(self, returncode: int, stderr: str):
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"agy process failed with exit code {returncode}: {stderr}")

# Robust Fallback Mappings between models
AGY_FALLBACK_CHAINS = {
    "Claude Opus 4.6 (Thinking)": [
        "Claude Sonnet 4.6 (Thinking)",
        "Gemini 3.1 Pro (High)",
        "Gemini 3.5 Flash (High)"
    ],
    "Claude Sonnet 4.6 (Thinking)": [
        "Gemini 3.1 Pro (High)",
        "Gemini 3.5 Flash (High)"
    ],
    "Gemini 3.1 Pro (High)": [
        "Gemini 3.1 Pro (Low)",
        "Gemini 3.5 Flash (High)"
    ],
    "Gemini 3.1 Pro (Low)": [
        "Gemini 3.5 Flash (High)"
    ],
    "Gemini 3.5 Flash (High)": [
        "Gemini 3.5 Flash (Medium)",
        "Gemini 3.5 Flash (Low)"
    ],
    "Gemini 3.5 Flash (Medium)": [
        "Gemini 3.5 Flash (High)",
        "Gemini 3.5 Flash (Low)"
    ],
    "Gemini 3.5 Flash (Low)": [
        "Gemini 3.5 Flash (High)"
    ],
    "GPT-OSS 120B (Medium)": [
        "Gemini 3.5 Flash (High)"
    ]
}

def get_fallback_chain(model_name: str) -> List[str]:
    """
    Returns the chain of fallback models for a given model.
    """
    resolved = resolve_agy_model(model_name)
    chain = AGY_FALLBACK_CHAINS.get(resolved, [])
    ultimate_fallback = "Gemini 3.5 Flash (High)"
    if resolved != ultimate_fallback and ultimate_fallback not in chain:
        chain = list(chain) + [ultimate_fallback]
    return chain

# SUBPROCESS RUNNER FOR AGY (Resilient & Stateful)
async def run_agy_subprocess(
    resolved_model: str, 
    prompt: str, 
    conversation_id: Optional[str] = None, 
    timeout: float = 90.0,
    http_request: Optional[Request] = None
) -> str:
    request_uuid = str(uuid.uuid4())
    tmp_home = utils.create_isolated_home(request_uuid)
    
    # State mapping for persistent conversations
    if conversation_id:
        utils.copy_conversation_db(conversation_id, tmp_home)
        
    env = os.environ.copy()
    env["HOME"] = tmp_home
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    
    sandbox_mode = os.environ.get("ANTIGRAVITY_WRAPPER_SANDBOX", "false").lower() == "true"
    cmd = ["agy", "--model", resolved_model, "--dangerously-skip-permissions"]
    if sandbox_mode:
        cmd.append("--sandbox")
    if conversation_id:
        cmd.extend(["--conversation", conversation_id])
    cmd.extend(["--prompt", prompt])
    
    # Concurrency Semaphore with client disconnect and timeout protection
    semaphore_acquired = False
    wait_start = time.time()
    while not semaphore_acquired:
        if http_request and await http_request.is_disconnected():
            logger.info("Client disconnected while waiting for completions semaphore.")
            raise asyncio.CancelledError("Client disconnected")
        if time.time() - wait_start > 30.0:
            raise asyncio.TimeoutError("Timeout waiting for completions semaphore slot")
            
        try:
            await asyncio.wait_for(agy_semaphore.acquire(), timeout=1.0)
            semaphore_acquired = True
        except asyncio.TimeoutError:
            continue
            
    try:
        logger.info(f"Spawning isolated agy process for req_uuid: {request_uuid} (Model: {resolved_model})")
        process = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except Exception:
                pass
            logger.error(f"agy process timed out after {timeout} seconds.")
            raise AgySubprocessError(-1, "Antigravity execution timed out.")
            
        if process.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace").strip()
            logger.error(f"agy process returned exit code {process.returncode}: {err_msg}")
            raise AgySubprocessError(process.returncode, err_msg)
            
        content = stdout.decode("utf-8", errors="replace")
        if not content.strip():
            logger.warning(f"agy process returned empty response for model {resolved_model}. Treating as failure.")
            raise AgySubprocessError(0, "Empty response from backend model. Possible rate limit or quota exceeded.")
            
        # Copy conversation DB back if successful
        if conversation_id:
            utils.map_new_conversation(tmp_home, conversation_id)
            utils.save_conversation_db(conversation_id, tmp_home)
            
        return content
        
    finally:
        if semaphore_acquired:
            agy_semaphore.release()
        utils.cleanup_isolated_home(tmp_home)

# HTTP PROXY RUNNER FOR NVIDIA NIM
async def proxy_to_nvidia_with_retry(payload: Dict[str, Any], timeout: float = 90.0) -> Dict[str, Any]:
    max_retries = 3
    base_delay = 1.0
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Proxying call to wrapper-nvidia (Attempt {attempt+1}/{max_retries})...")
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "http://127.0.0.1:9100/v1/chat/completions",
                    json=payload,
                    timeout=timeout
                )
                
                if response.status_code == 429 or response.status_code >= 500:
                    logger.warning(f"wrapper-nvidia returned error status {response.status_code}. Retrying...")
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                    await asyncio.sleep(delay)
                    continue
                    
                response.raise_for_status()
                return response.json()
                
        except Exception as e:
            logger.error(f"Connection error to wrapper-nvidia on attempt {attempt+1}: {e}")
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            await asyncio.sleep(delay)
            
    raise HTTPException(status_code=502, detail="Failed to get successful response from wrapper-nvidia after retries.")

# STREAM GENERATOR FOR AGY (Checks Client Disconnection & Startup Success)
async def generate_agy_stream(
    resolved_model: str,
    prompt: str,
    conversation_id: Optional[str],
    req_id: str,
    request: Request,
    timeout: float = 120.0
):
    request_uuid = str(uuid.uuid4())
    tmp_home = utils.create_isolated_home(request_uuid)
    
    if conversation_id:
        utils.copy_conversation_db(conversation_id, tmp_home)
        
    env = os.environ.copy()
    env["HOME"] = tmp_home
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    
    sandbox_mode = os.environ.get("ANTIGRAVITY_WRAPPER_SANDBOX", "false").lower() == "true"
    cmd = ["agy", "--model", resolved_model, "--dangerously-skip-permissions"]
    if sandbox_mode:
        cmd.append("--sandbox")
    if conversation_id:
        cmd.extend(["--conversation", conversation_id])
    cmd.extend(["--prompt", prompt])
    
    semaphore_acquired = False
    wait_start = time.time()
    while not semaphore_acquired:
        if await request.is_disconnected():
            logger.info("Client disconnected while waiting for stream semaphore slot.")
            return
        if time.time() - wait_start > 30.0:
            raise asyncio.TimeoutError("Timeout waiting for stream semaphore slot")
            
        try:
            await asyncio.wait_for(agy_semaphore.acquire(), timeout=1.0)
            semaphore_acquired = True
        except asyncio.TimeoutError:
            continue
            
    process = None
    try:
        logger.info(f"Spawning isolated stream agy process for req_uuid: {request_uuid} (Model: {resolved_model})")
        process = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        # Verify initial process startup success before yielding anything
        start_time = time.time()
        first_line = b""
        try:
            # Read first line with timeout to confirm output starts
            read_line_task = process.stdout.readline()
            first_line = await asyncio.wait_for(read_line_task, timeout=20.0)
        except asyncio.TimeoutError:
            if process.returncode is not None and process.returncode != 0:
                stderr = await process.stderr.read()
                err_msg = stderr.decode("utf-8", errors="replace").strip()
                raise AgySubprocessError(process.returncode, err_msg)
            raise asyncio.TimeoutError("Timeout waiting for process output to start")
            
        if not first_line:
            await process.wait()
            if process.returncode != 0:
                stderr = await process.stderr.read()
                err_msg = stderr.decode("utf-8", errors="replace").strip()
                raise AgySubprocessError(process.returncode, err_msg)
            else:
                raise AgySubprocessError(0, "Empty stream received from backend model. Possible rate limit or quota exceeded.")
                
        # Check if the first line is a warning. If so, discard it and read the next line/chunk!
        first_line_str = first_line.decode("utf-8", errors="replace")
        if first_line_str.strip().startswith("Warning: conversation") and "not found" in first_line_str:
            # Check if there is a next chunk of real output
            try:
                next_task = process.stdout.read(64)
                next_chunk = await asyncio.wait_for(next_task, timeout=15.0)
                if not next_chunk:
                    await process.wait()
                    if process.returncode != 0:
                        stderr = await process.stderr.read()
                        err_msg = stderr.decode("utf-8", errors="replace").strip()
                        raise AgySubprocessError(process.returncode, err_msg)
                    else:
                        raise AgySubprocessError(0, "Empty stream after warning line. Possible rate limit or quota exceeded.")
                
                # Yield the valid first content chunk
                text = next_chunk.decode("utf-8", errors="replace")
                chunk_data = {
                    "id": req_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": resolved_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": text},
                            "finish_reason": None
                        }
                    ]
                }
                yield f"data: {import_json_dump(chunk_data)}\n\n"
            except asyncio.TimeoutError:
                if process.returncode is not None and process.returncode != 0:
                    stderr = await process.stderr.read()
                    err_msg = stderr.decode("utf-8", errors="replace").strip()
                    raise AgySubprocessError(process.returncode, err_msg)
                raise asyncio.TimeoutError("Timeout waiting for process output to start after warning line")
        else:
            # It's a valid completion line, yield it!
            if first_line:
                chunk_data = {
                    "id": req_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": resolved_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": first_line_str},
                            "finish_reason": None
                        }
                    ]
                }
                yield f"data: {import_json_dump(chunk_data)}\n\n"
            
        # Stream the remaining output
        while True:
            if await request.is_disconnected():
                logger.info("Client disconnected. Killing agy stream subprocess.")
                try:
                    process.kill()
                except Exception:
                    pass
                break
                
            try:
                read_task = process.stdout.read(64)
                chunk = await asyncio.wait_for(read_task, timeout=10.0)
            except asyncio.TimeoutError:
                if process.returncode is not None:
                    break
                if time.time() - start_time > timeout:
                    logger.warning("Stream hit execution timeout limit. Killing process.")
                    try:
                        process.kill()
                    except Exception:
                        pass
                    break
                continue
                
            if not chunk:
                break
                
            text = chunk.decode("utf-8", errors="replace")
            chunk_data = {
                "id": req_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": resolved_model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": text},
                        "finish_reason": None
                    }
                ]
            }
            yield f"data: {import_json_dump(chunk_data)}\n\n"
            
        stderr = await process.stderr.read()
        if stderr:
            logger.error(f"agy stream stderr: {stderr.decode('utf-8', errors='replace')}")
            
        await process.wait()
        if process.returncode != 0:
            logger.error(f"agy stream process exited with error code {process.returncode}")
        else:
            if conversation_id:
                utils.map_new_conversation(tmp_home, conversation_id)
                utils.save_conversation_db(conversation_id, tmp_home)
                
        # Final stop chunk
        stop_chunk = {
            "id": req_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": resolved_model,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop"
                }
            ]
        }
        yield f"data: {import_json_dump(stop_chunk)}\n\n"
        yield "data: [DONE]\n\n"
        
    finally:
        if semaphore_acquired:
            agy_semaphore.release()
        utils.cleanup_isolated_home(tmp_home)

# STREAM COMPLETION RUNNER WITH MULTI-MODEL FALLBACK AND RETRY
async def run_agy_stream_with_fallback(
    primary_model: str,
    messages: List[ChatMessage],
    conversation_id: Optional[str],
    req_id: str,
    http_request: Request
):
    model_chain = [primary_model] + get_fallback_chain(primary_model)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_chain = []
    for m in model_chain:
        if m not in seen:
            seen.add(m)
            unique_chain.append(m)
            
    yielded_chunks = False
    last_exception = None
    
    for model_name in unique_chain:
        prompt = utils.extract_text_content(messages[-1].content) if conversation_id else utils.format_messages(messages)
        
        max_attempts = 2
        for attempt in range(max_attempts):
            if await http_request.is_disconnected():
                logger.info("Client disconnected. Stopping stream fallback attempts.")
                return
                
            try:
                logger.info(f"Attempting AGY stream with model '{model_name}' (Attempt {attempt+1}/{max_attempts})")
                async for chunk in generate_agy_stream(model_name, prompt, conversation_id, req_id, http_request):
                    yielded_chunks = True
                    yield chunk.encode("utf-8")
                return
            except Exception as e:
                last_exception = e
                logger.warning(f"AGY stream attempt {attempt+1} failed for model '{model_name}': {e}")
                
                if yielded_chunks:
                    logger.error(f"Stream failed after yielding chunks: {e}. Raising error chunk.")
                    error_chunk = {
                        "error": {
                            "message": f"Stream failed mid-stream: {str(e)}",
                            "type": "server_error",
                            "code": 500
                        }
                    }
                    yield f"data: {import_json_dump(error_chunk)}\n\n".encode("utf-8")
                    yield b"data: [DONE]\n\n"
                    return
                    
                if attempt < max_attempts - 1:
                    delay = 1.0 * (2 ** attempt) + random.uniform(0.1, 0.5)
                    await asyncio.sleep(delay)
                    
        logger.warning(f"Model '{model_name}' stream exhausted in fallback chain. Moving to next fallback model.")
        
    # If everything failed and no chunks yielded, return clean error block
    if not yielded_chunks:
        error_chunk = {
            "error": {
                "message": f"All stream fallback attempts failed. Last error: {str(last_exception)}",
                "type": "server_error",
                "code": 502
            }
        }
        yield f"data: {import_json_dump(error_chunk)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"

# UNIFIED ROUTING & FAILOVER STREAM GENERATOR
async def unified_chat_completion_stream(
    request: ChatCompletionRequest,
    req_id: str,
    http_request: Request
):
    conversation_id = http_request.headers.get("X-Conversation-ID")
    resolved_model = resolve_agy_model(request.model)
    async for chunk in run_agy_stream_with_fallback(resolved_model, request.messages, conversation_id, req_id, http_request):
        yield chunk

@app.get("/health")
@app.get("/")
def health_check():
    return {"status": "healthy", "service": "antigravity-wrapper"}

@app.get("/v1/models")
async def list_models():
    """
    Returns list of all available models, dynamically loaded from 'agy models'
    and enriched with context windows, owned_by, and capabilities metadata.
    """
    await fetch_agy_models_dynamically()
    
    models_list = []
    seen_ids = set()
    
    # 1. Native dynamic models from agy
    for model_id in AGY_MODELS_CACHE:
        if model_id in seen_ids:
            continue
        seen_ids.add(model_id)
        models_list.append(classify_agy_model(model_id))
        
    # 2. Helpful standard aliases
    for alias, canonical in MODEL_MAPPING.items():
        if alias in seen_ids:
            continue
        if canonical in AGY_MODELS_CACHE:
            seen_ids.add(alias)
            model_info = classify_agy_model(canonical)
            model_info["id"] = alias  # override ID with the alias
            models_list.append(model_info)
            
    return {
        "object": "list",
        "data": models_list
    }

# NON-STREAM COMPLETION RUNNER WITH MULTI-MODEL FALLBACK AND RETRY
async def run_agy_with_fallback(
    primary_model: str,
    messages: List[ChatMessage],
    conversation_id: Optional[str],
    original_model_requested: str,
    http_request: Request
) -> Dict[str, Any]:
    model_chain = [primary_model] + get_fallback_chain(primary_model)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_chain = []
    for m in model_chain:
        if m not in seen:
            seen.add(m)
            unique_chain.append(m)
            
    last_exception = None
    
    for model_name in unique_chain:
        prompt = utils.extract_text_content(messages[-1].content) if conversation_id else utils.format_messages(messages)
        
        max_attempts = 2
        for attempt in range(max_attempts):
            if await http_request.is_disconnected():
                logger.info("Client disconnected. Cancelling completions execution.")
                raise HTTPException(status_code=499, detail="Client Closed Request")
                
            try:
                logger.info(f"Attempting AGY completions with model '{model_name}' (Attempt {attempt+1}/{max_attempts})")
                content = await run_agy_subprocess(model_name, prompt, conversation_id, http_request=http_request)
                return format_openai_response(content, original_model_requested)
            except Exception as e:
                last_exception = e
                logger.warning(f"AGY completions attempt {attempt+1} failed for model '{model_name}': {e}")
                if attempt < max_attempts - 1:
                    delay = 1.0 * (2 ** attempt) + random.uniform(0.1, 0.5)
                    await asyncio.sleep(delay)
                    
        logger.warning(f"Model '{model_name}' exhausted in fallback chain. Moving to next fallback model.")
        
    logger.error(f"Completions failed completely. Last exception: {last_exception}")
    raise HTTPException(
        status_code=502,
        detail=f"All completion fallback models failed. Last error: {str(last_exception)}"
    )

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest, http_request: Request):
    req_id = f"chatcmpl-{uuid.uuid4()}"
    conversation_id = http_request.headers.get("X-Conversation-ID")
    
    if request.stream:
        return StreamingResponse(
            unified_chat_completion_stream(request, req_id, http_request),
            media_type="text/event-stream"
        )
    else:
        resolved_model = resolve_agy_model(request.model)
        return await run_agy_with_fallback(resolved_model, request.messages, conversation_id, request.model, http_request)
