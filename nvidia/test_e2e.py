import requests
import json
import time

BASE_URL = "http://localhost:9100"

def test_chat_completions():
    print("\n--- Test Chat Completions (meta/llama-3.1-8b-instruct) ---")
    payload = {
        "model": "meta/llama-3.1-8b-instruct",
        "messages": [{"role": "user", "content": "Hello! Reply with exactly the word 'SUCCESS' and nothing else."}]
    }
    r = requests.post(f"{BASE_URL}/v1/chat/completions", json=payload)
    print("Status:", r.status_code)
    try:
        res = r.json()
        print("Reply:", res["choices"][0]["message"]["content"])
    except Exception as e:
        print("Error:", e, r.text)

def test_chat_completions_streaming():
    print("\n--- Test Chat Completions Streaming (meta/llama-3.1-8b-instruct) ---")
    payload = {
        "model": "meta/llama-3.1-8b-instruct",
        "messages": [{"role": "user", "content": "Hello! Reply with exactly the word 'SUCCESS' and nothing else."}],
        "stream": True
    }
    r = requests.post(f"{BASE_URL}/v1/chat/completions", json=payload, stream=True)
    print("Status:", r.status_code)
    for line in r.iter_lines():
        if line:
            decoded = line.decode('utf-8')
            if decoded.startswith("data: "):
                data_str = decoded[6:]
                if data_str.strip() == "[DONE]":
                    print("[DONE]")
                    break
                try:
                    data = json.loads(data_str)
                    content = data["choices"][0]["delta"].get("content", "")
                    if content:
                        print(content, end="", flush=True)
                except Exception as e:
                    pass
    print()

def test_messages_anthropic():
    print("\n--- Test Messages Anthropic (meta/llama-3.1-8b-instruct) ---")
    payload = {
        "model": "meta/llama-3.1-8b-instruct",
        "messages": [{"role": "user", "content": "Hello! Reply with exactly the word 'SUCCESS' and nothing else."}]
    }
    r = requests.post(f"{BASE_URL}/v1/messages", json=payload)
    print("Status:", r.status_code)
    try:
        res = r.json()
        print("Reply:", res["content"][0]["text"])
    except Exception as e:
        print("Error:", e, r.text)

def test_embeddings():
    print("\n--- Test Embeddings (nvidia/nv-embed-v1) ---")
    payload = {
        "model": "nvidia/nv-embed-v1",
        "input": "Hello world",
        "input_type": "query"
    }
    r = requests.post(f"{BASE_URL}/v1/embeddings", json=payload)
    print("Status:", r.status_code)
    try:
        res = r.json()
        print("Embedding Vector Length:", len(res["data"][0]["embedding"]))
    except Exception as e:
        print("Error:", e, r.text)

def test_image_generations_clamp():
    print("\n--- Test Image Generations Clamping (black-forest-labs/flux.1-schnell) ---")
    # Request width=512, height=512 -> should be clamped/enforced to 768
    payload = {
        "model": "black-forest-labs/flux.1-schnell",
        "prompt": "a beautiful green apple",
        "width": 512,
        "height": 512
    }
    r = requests.post(f"{BASE_URL}/v1/images/generations", json=payload)
    print("Status:", r.status_code)
    try:
        res = r.json()
        # Flux outputs b64_json
        img_b64 = res["data"][0]["b64_json"]
        print("Image base64 length:", len(img_b64))
    except Exception as e:
        print("Error:", e, r.text)

def test_retired_model():
    print("\n--- Test Retired Model Isolation (google/gemma-3n-e4b-it) ---")
    payload = {
        "model": "google/gemma-3n-e4b-it",
        "messages": [{"role": "user", "content": "Hello!"}]
    }
    r = requests.post(f"{BASE_URL}/v1/chat/completions", json=payload)
    print("Status:", r.status_code)
    print("Body:", r.text)

def test_vision_image_conversion():
    print("\n--- Test Vision Image URL Conversion (meta/llama-3.2-11b-vision-instruct) ---")
    payload = {
        "model": "meta/llama-3.2-11b-vision-instruct",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this image?"},
                    {"type": "image_url", "image_url": {"url": "https://www.google.com/images/branding/googlelogo/1x/googlelogo_color_272x92dp.png"}}
                ]
            }
        ],
        "max_tokens": 100
    }
    r = requests.post(f"{BASE_URL}/v1/chat/completions", json=payload)
    print("Status:", r.status_code)
    try:
        res = r.json()
        print("Reply:", res["choices"][0]["message"]["content"])
    except Exception as e:
        print("Error:", e, r.text)

if __name__ == "__main__":
    test_chat_completions()
    test_chat_completions_streaming()
    test_messages_anthropic()
    test_embeddings()
    test_image_generations_clamp()
    test_retired_model()
    test_vision_image_conversion()
