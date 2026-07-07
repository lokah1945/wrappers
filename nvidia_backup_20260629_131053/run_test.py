import requests
import json

payload = {
    "model": "meta/llama-3.1-8b-instruct",
    "messages": [{"role": "user", "content": "hello"}]
}
try:
    r = requests.post("http://127.0.0.1:9100/v1/chat/completions", json=payload, timeout=10)
    print("Status:", r.status_code)
    print("Response:", r.text)
except Exception as e:
    print("Error:", e)
