# Production audit report

- Timestamp: `2026-07-25T02:16:43.366615+07:00`
- Repository: `/root/wrapper`
- Mode: safe preflight plus explicitly requested tests/smoke/load

## Results
- **PASS** — repository layout: wrappers.json present
- **PASS** — deployed commit: 0213768664427331d03fff55ab46f32c3a492e3a
- **PASS** — git branch: main
- **PASS** — git origin: configured
- **PASS** — working tree: clean
- **PASS** — installer executable: /root/wrapper/install.sh
- **PASS** — directory common: /root/wrapper/common
- **PASS** — directory model-registry: /root/wrapper/model-registry
- **PASS** — directory nvidia-python: /root/wrapper/nvidia-python
- **PASS** — directory nous: /root/wrapper/nous
- **PASS** — directory opencode: /root/wrapper/opencode
- **PASS** — directory blackbox: /root/wrapper/blackbox
- **PASS** — no model substitution markers: none
- **PASS** — config presence nvidia-python/.env: present
- **PASS** — config presence nous/.env: present
- **PASS** — config presence opencode/.env: present
- **PASS** — config presence blackbox/.env: present
- **PASS** — config presence model-registry/.env: present
- **FAIL** — systemd wrapper-model-registry.service: inactive
- **BLOCKED** — systemd wrapper-nvidia-python.service: inactive
- **FAIL** — systemd wrapper-nous.service: inactive
- **FAIL** — systemd wrapper-opencode.service: inactive
- **FAIL** — systemd wrapper-blackbox.service: inactive
- **PASS** — endpoint registry: HTTP 200, 4.8 ms, status=ok
- **FAIL** — orphan runtime registry: endpoint is healthy but its systemd unit is not active
- **PASS** — endpoint nvidia: HTTP 200, 3.7 ms, status=ok
- **BLOCKED** — runtime commit nvidia: health response has no git_commit/build identity
- **PASS** — endpoint nous: HTTP 200, 1.7 ms, status=ok
- **FAIL** — orphan runtime nous: endpoint is healthy but its systemd unit is not active
- **BLOCKED** — runtime commit nous: health response has no git_commit/build identity
- **PASS** — endpoint opencode: HTTP 200, 1.4 ms, status=ok
- **FAIL** — orphan runtime opencode: endpoint is healthy but its systemd unit is not active
- **BLOCKED** — runtime commit opencode: health response has no git_commit/build identity
- **FAIL** — endpoint blackbox: HTTP 0, 0.3 ms, <urlopen error [Errno 111] Connection refused>
- **FAIL** — repository tests: = asyncio.run(kp.acquire())["key"]
            second = asyncio.run(kp.acquire())["key"]
            assert first.label != second.label
            kp.release(first)
            kp.release(second)
            kp.mark_failure(first, 429, retry_after=60)
            third = asyncio.run(kp.acquire())["key"]
>           assert third.label == second.label
E           AssertionError: assert 'key5' == 'key3'
E
E             - key3
E             ?    ^
E             + key5
E             ?    ^

tests/test_agent_runtime_contracts.py:205: AssertionError
------------------------------ Captured log call -------------------------------
WARNING  wrapper-opencode:key_pool.py:97 [opencode] key key1 cooled down for 60s (rate_limit)
_____ test_opencode_proxy_retries_next_key_before_returning_upstream_error _____

    def test_opencode_proxy_retries_next_key_before_returning_upstream_error():
        oc = _load_opencode()
        old_pool = oc.pool
        old_proxy = oc.proxy_request
        old_env = {k: os.environ.get(k) for k in ("OPENCODE_API_KEY_1", "OPENCODE_API_KEY_2")}
        os.environ["OPENCODE_API_KEY_1"] = "sk-[REDACTED]"
        os.environ["OPENCODE_API_KEY_2"] = "sk-[REDACTED]"
        calls = []

        async def fake_proxy(method, url, json_body=None, headers=None, is_stream=False):
            token = (headers or {}).get("Authorization", "").replace("Bearer ", "")
            calls.append(token)
            if token == "sk-[REDACTED]":
                return 429, {"error": {"message": "rate limited", "type": "rate_limit_error"}}
            return 200, {"choices": [{"message": {"content": "ok"}}]}

        class Req:
            headers = {}

        try:
            oc.pool = oc.KeyPool().load_from_env()
            oc.proxy_request = fake_proxy
            status, data, key = asyncio.run(oc.proxy_request_with_pool("POST", "https://example.test/chat/completions", {"model": "m"}, Req()))
        finally:
            oc.pool = old_pool
            oc.proxy_request = old_proxy
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        assert status == 200
        assert key is None
>       assert calls == ["sk-[REDACTED]", "sk-[REDACTED]"]
E       AssertionError: assert ['sk-[REDACTED]'] == ['sk-[REDACTED]']
E
E         At index 1 diff: 'sk-[REDACTED]' != 'sk-[REDACTED]'
E         Use -v to get more diff

tests/test_agent_runtime_contracts.py:272: AssertionError
------------------------------ Captured log call -------------------------------
WARNING  wrapper-opencode:key_pool.py:97 [opencode] key key1 cooled down for 65s (rate_limit)
______________ test_blackbox_proxy_retries_next_key_before_error _______________

    def test_blackbox_proxy_retries_next_key_before_error():
        bb = _load_blackbox()
        old_pool = bb.pool
        old_proxy = bb.proxy_request
        old_env = {k: os.environ.get(k) for k in ("BLACKBOX_API_KEY_1", "BLACKBOX_API_KEY_2")}
        os.environ["BLACKBOX_API_KEY_1"] = "sk-[REDACTED]"
        os.environ["BLACKBOX_API_KEY_2"] = "sk-[REDACTED]"
        calls = []

        async def fake_proxy(method, url, json_body=None, headers=None, is_stream=False):
            token = (headers or {}).get("Authorization", "").replace("Bearer ", "")
            calls.append(token)
            if token == "sk-[REDACTED]":
                return 429, {"error": {"message": "rate limited", "type": "rate_limit_error"}}
            return 200, {"choices": [{"message": {"content": "ok"}}]}

        class Req:
            headers = {}

        try:
            bb.pool = bb.KeyPool().load_from_env()
            bb.proxy_request = fake_proxy
            status, data, key = asyncio.run(bb.proxy_request_with_pool("POST", "https://example.test/chat/completions", {"model": "m"}, Req()))
        finally:
            bb.pool = old_pool
            bb.proxy_request = old_proxy
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        assert status == 200
        assert key is None
>       assert calls == ["sk-[REDACTED]", "sk-[REDACTED]"]
E       AssertionError: assert ['sk-[REDACTED]'] == ['sk-[REDACTED]']
E
E         At index 1 diff: 'sk-[REDACTED]' != 'sk-[REDACTED]'
E         Use -v to get more diff

tests/test_agent_runtime_contracts.py:346: AssertionError
------------------------------ Captured log call -------------------------------
WARNING  wrapper-blackbox:key_pool.py:87 [blackbox] key key1 cooled down for 65s (rate_limit)
__________ test_disabled_client_does_not_enqueue_or_open_connections ___________

    def test_disabled_client_does_not_enqueue_or_open_connections():
        client = ModelRegistryClient("")
        client.schedule_observation("nvidia", "provider/model-a", "scope", "available", 200, "OK", "", "chat")
        assert client._queue.qsize() == 0
>       assert client.enabled is False
E       assert True is False
E        +  where True = <common.model.central_client.ModelRegistryClient object at 0x7fc75c7db150>.enabled

tests/test_central_client.py:24: AssertionError
=========================== short test summary info ============================
FAILED tests/test_agent_runtime_contracts.py::test_opencode_key_pool_rotates_and_skips_cooled_down_key
FAILED tests/test_agent_runtime_contracts.py::test_opencode_proxy_retries_next_key_before_returning_upstream_error
FAILED tests/test_agent_runtime_contracts.py::test_blackbox_proxy_retries_next_key_before_error
FAILED tests/test_central_client.py::test_disabled_client_does_not_enqueue_or_open_connections
4 failed, 68 passed in 5.91s

- **PASS** — cross-wrapper transparency: NV A→O OK
NV O→A OK
NV STREAM OK
NOUS OK
OPENCODE OK
ALL CROSS-WRAPPER TRANSPARENCY CHECKS PASS


## Summary
- PASS: `23`
- FAIL: `9`
- BLOCKED: `4`

## Interpretation
- BLOCKED means the VPS did not provide the required service/configuration or an explicit test flag was not supplied.
- FAIL means an available component violated an acceptance criterion.
- A production-ready decision requires zero FAIL and no unreviewed BLOCKED result.
- The report intentionally does not include secrets or response bodies.
