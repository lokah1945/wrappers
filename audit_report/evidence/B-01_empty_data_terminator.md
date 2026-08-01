# Evidence Artifact: B-01 Empty Data Terminator Bug

**Finding:** B-01 — blackbox/opencode treat empty `data:` as stream terminator

## Source Code Evidence

### blackbox/src/main.py:1445
```python
# In _responses_stream generator
if payload in (b'[DONE]', b'', b'"[DONE]"'):
    for ev in state.force_done():
        yield ev
    return  # TERMINATES STREAM ON EMPTY PAYLOAD
```

### blackbox/src/main.py:1621
```python
# In anthropic stream generator
if payload in (b'[DONE]', b'', b'"[DONE]"'):
    for ev in state.force_done():
        yield ev
    return  # TERMINATES STREAM ON EMPTY PAYLOAD
```

### opencode/src/main.py:1633
```python
# In Responses stream
if payload in (b"[DONE]", b"", b'"[DONE]"'):
    done = True
    break
```

### opencode/src/main.py:1829
```python
# In Anthropic stream
if payload in (b"[DONE]", b""):
    for ev in state.force_done():
        yield ev
    return
```

## Correct Implementation (nous/src/main.py:1288-1291)
```python
# N-09 fix: empty data: is keep-alive, NOT end-of-stream
if payload == b"":
    continue  # Skip, do not terminate
if payload in (b'[DONE]', b'"[DONE]"'):
    for ev in state.force_done():
        yield ev
    return
```

## Test Verification
```bash
$ python -m pytest tests/test_sse_streaming_regressions.py::test_b01_empty_data_line_is_keepalive_not_terminator -v
PASSED

$ python -m pytest tests/test_sse_streaming_regressions.py::test_b01_state_machine_survives_blank_delta -v
PASSED
```

## Impact
Upstream keep-alive frames (empty `data:`) cause premature stream termination → "proses berhenti di tengah jalan"