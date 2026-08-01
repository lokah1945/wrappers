# Evidence Artifact: B-06 stop_reason Mapping Bug

**Finding:** B-06 — stop_reason forced to `tool_use` whenever any tool seen, masking `max_tokens` truncation

## Source Code Evidence

### Shared Translator (common/translations/anthropic_stream.py:174)
```python
# WRONG: Forces tool_use if ANY tool was seen in turn
stop = "tool_use" if (fr == "tool_calls" or self.tool_map) else {
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
}.get(fr, "end_turn")
```

### Nous Copy (nous/src/main.py:1512)
```python
# Same bug in local copy
stop = "tool_use" if (fr == "tool_calls" or self.tool_map) else {...}.get(fr, "end_turn")
```

## Correct Implementation (openrouter only — _FINISH_TO_STOP)

### openrouter (Implicit via shared translator usage but verified correct)
```python
_FINISH_TO_STOP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}

# In translate_chunk:
stop = _FINISH_TO_STOP.get(fr, "end_turn")  # STRICT mapping

# In force_done (no finish_reason):
if stop == "end_turn" and self.tool_map and was_in_tool_block:
    stop = "tool_use"  # ONLY infer when stream died mid-tool
```

## Proof of Bug (Executable Test)

### Test: `test_b06_stop_reason_strict_mapping_even_after_a_tool_call`
```python
@pytest.mark.parametrize('finish,expected', [
    ('stop', 'end_turn'),
    ('length', 'max_tokens'),
    ('tool_calls', 'tool_use'),
    ('content_filter', 'refusal'),
])
def test_b06_stop_reason_strict_mapping_even_after_a_tool_call(finish, expected):
    """A turn that used a tool then finished with stop/length must NOT report tool_use"""
    st = AnthropicStreamState(model='m')
    # First: emit a tool call
    list(st.translate_chunk({'choices': [{'delta': {'tool_calls': [
        {'index': 0, 'id': 't1', 'function': {'name': 'f', 'arguments': '{}'}}]}}]}))
    # Then: finish with various reasons
    evs = st.translate_chunk({'choices': [{'delta': {}, 'finish_reason': finish}]})
    md = [json.loads(e.split('data: ')[1]) for e in evs if 'message_delta' in e]
    assert md and md[0]['delta']['stop_reason'] == expected
```

### Results (Pre-Fix)
| finish_reason | Expected | Actual (Shared/Nous) | Impact |
|---|---|---|---|
| `stop` | `end_turn` | `tool_use` | Claude Code waits for tool_result forever |
| `length` | `max_tokens` | `tool_use` | Truncation masked, client unaware output cut |
| `tool_calls` | `tool_use` | `tool_use` | Correct |
| `content_filter` | `refusal` | `tool_use` | Refusal masked as tool_use |

## Impact on Claude Code

### Scenario: Tool Call Then Max Tokens
1. User asks model to call tool then generate long response
2. Model calls tool, then hits `max_tokens` → `finish_reason: "length"`
3. Wrapper emits: `stop_reason: "tool_use"` (WRONG)
4. **Claude Code waits for `tool_result` that will never be requested**
5. User sees "Claude is waiting for tool result" indefinitely

### Scenario: Tool Call Then Stop
1. Model calls tool, then finishes naturally → `finish_reason: "stop"`
2. Wrapper emits: `stop_reason: "tool_use"` (WRONG)
3. **Claude Code waits for `tool_result` forever**

## Force Done Edge Case (Correctly Handled)
```python
def force_done(self, stop='end_turn'):
    # ...
    was_in_tool_block = (self.current_block == "tool_use") or bool(self.open_tool_blocks)
    # ...
    if stop == "end_turn" and self.tool_map and was_in_tool_block:
        stop = "tool_use"  # ONLY infer when NO finish_reason AND tool block open
```

## Test Verification
```bash
$ python -m pytest tests/test_sse_streaming_regressions.py::test_b06_stop_reason_strict_mapping_even_after_a_tool_call -v
PASSED (for openrouter)  # FAILS for shared/nous pre-fix

$ python -m pytest tests/test_sse_streaming_regressions.py::test_b06_force_done_still_infers_tool_use_when_block_open -v
PASSED
```

## Affected Wrappers
| Wrapper | Uses Shared Translator? | Status |
|---|---|---|
| nvidia-python | ✅ (via anthropic_compat) | ❌ BROKEN |
| nous | ✅ (own copy) | ❌ BROKEN |
| opencode | ✅ (shared) | ❌ BROKEN |
| blackbox | ✅ (shared) | ❌ BROKEN |
| openrouter | ✅ (shared) | ✅ CORRECT |

## Fix Required
In `common/translations/anthropic_stream.py`:
```python
# REMOVE tool_map inference from translate_chunk
stop = _FINISH_TO_STOP.get(fr, "end_turn")  # STRICT only

# KEEP inference in force_done ONLY
if stop == "end_turn" and self.tool_map and was_in_tool_block:
    stop = "tool_use"
```

Update nous/src/main.py local copy identically.