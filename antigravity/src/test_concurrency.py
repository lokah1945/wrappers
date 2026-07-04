import json
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

def send_request(index):
    url = "http://127.0.0.1:9101/v1/chat/completions"
    prompt = f"Perform the math calculation: {index} + 1. Respond with only the final number."
    payload = {
        "model": "antigravity",
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "stream": False
    }
    
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    
    start_time = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            res_body = response.read().decode("utf-8")
            res_json = json.loads(res_body)
            content = res_json["choices"][0]["message"]["content"].strip()
            duration = time.time() - start_time
            return index, content, duration, None
    except Exception as e:
        duration = time.time() - start_time
        return index, None, duration, str(e)

def main():
    print("Starting concurrency test: 20 simultaneous requests...")
    num_requests = 20
    start_all = time.time()
    
    results = []
    with ThreadPoolExecutor(max_workers=num_requests) as executor:
        futures = {executor.submit(send_request, i): i for i in range(1, num_requests + 1)}
        for future in as_completed(futures):
            results.append(future.result())
            
    print("\n--- Concurrency Test Results ---")
    success_count = 0
    for index, content, duration, error in sorted(results, key=lambda x: x[0]):
        expected = str(index + 1)
        if error:
            print(f"Req #{index:02d}: FAILED (Error: {error}) in {duration:.2f}s")
        elif expected in content:
            print(f"Req #{index:02d}: SUCCESS (Expected: {expected}, Got: {content}) in {duration:.2f}s")
            success_count += 1
        else:
            print(f"Req #{index:02d}: MISMATCH (Expected: {expected}, Got: {content}) in {duration:.2f}s")
            
    total_duration = time.time() - start_all
    print(f"\nCompleted: {success_count}/{num_requests} requests succeeded.")
    print(f"Total time taken: {total_duration:.2f} seconds.")
    
    if success_count == num_requests:
        print("\nSUCCESS: All concurrent requests returned isolated and correct answers without race conditions!")
    else:
        print("\nFAILURE: Some concurrent requests failed or returned mismatched answers.")

if __name__ == "__main__":
    main()
