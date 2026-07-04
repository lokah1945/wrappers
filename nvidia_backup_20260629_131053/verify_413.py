import requests
try:
    r = requests.post('http://127.0.0.1:9100/v1/chat/completions', data='X' * (30 * 1024 * 1024), timeout=15)
    print("Status:", r.status_code)
    print("Response:", r.text)
except Exception as e:
    print("Error:", e)
