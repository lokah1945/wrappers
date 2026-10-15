#!/usr/bin/env python3
"""PATCH: thinking content blocks must carry a signature (Anthropic contract).

Root cause: all wrappers emit `thinking` blocks (content_block_start +
thinking_delta + content_block_stop) but NEVER the signature_delta /
`signature` field. Real Anthropic responses always carry one; strict SDKs
and Claude Code interleaved-thinking round-trips expect a structurally
valid block (thinking blocks are passed back verbatim on tool continuation).

Fix: shared helper `thinking_signature()` in common/translations/think_tags.py
(deterministic synthetic opaque token), wired into EVERY thinking-block close
site across: nvidia live path (anthropic_compat), nvidia test path (main),
nous (stream + non-stream), openrouter (stream + non-stream), shared
translations (anthropic_stream + shared non-stream).

Every replacement verifies its expected occurrence count; backups first.
"""
import shutil
import sys
import time

BASE = "/root/wrapper"
STAMP = time.strftime("%Y%m%d-%H%M%S")

# (path, old, new, expected_count)
PATCHES = []


def P(path, old, new, count=1):
    PATCHES.append((f"{BASE}/{path}", old, new, count))


# ---------------------------------------------------------------- think_tags
TT = "common/translations/think_tags.py"
P(TT,
  "from __future__ import annotations\n\nimport re\n",
  "from __future__ import annotations\n\nimport base64\nimport re\n")
THINK_TAGS_APPEND = '''

# ---------------------------------------------------------------------------
# Anthropic thinking-block signature (contract completeness)
# ---------------------------------------------------------------------------
# The Anthropic contract requires every `thinking` content block to carry a
# `signature` (streamed as a `signature_delta` immediately before
# `content_block_stop`, present as a field on non-stream blocks). Real
# Anthropic signatures are opaque server-signed tokens; the wrapper emits a
# deterministic synthetic token so strict SDKs and Claude Code round-trips
# (interleaved thinking blocks are passed back verbatim on tool
# continuation) always see a structurally valid block. The token is opaque
# to clients and accepted verbatim on the way back in.
_THINKING_SIGNATURE = base64.b64encode(b"wrapper-thinking-v1").decode("ascii")


def thinking_signature() -> str:
    """Synthetic opaque signature for thinking content blocks."""
    return _THINKING_SIGNATURE


def signature_delta_event(index: int) -> dict:
    """Ready-to-emit content_block_delta payload for a thinking block."""
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "signature_delta", "signature": _THINKING_SIGNATURE},
    }
'''

# ------------------------------------------------- nvidia anthropic_compat
NVAC = "nvidia-python/src/anthropic_compat.py"
P(NVAC,
  "from common.translations.think_tags import ThinkTagStreamFilter, strip_trailing_think_tags",
  "from common.translations.think_tags import ThinkTagStreamFilter, strip_trailing_think_tags, thinking_signature")
# 1. stop_open() — single choke point for closing the open text/thinking block
P(NVAC,
  "        nonlocal open_idx, text_index, thinking_index\n"
  "        if open_idx is not None and open_idx not in set(tool_map.values()):\n"
  "            yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': open_idx})\n",
  "        nonlocal open_idx, text_index, thinking_index\n"
  "        if open_idx is not None and open_idx not in set(tool_map.values()):\n"
  "            if open_idx == thinking_index:\n"
  "                # Anthropic contract: thinking blocks carry a signature\n"
  "                # (signature_delta immediately before content_block_stop).\n"
  "                yield _sse('content_block_delta', {\n"
  "                    'type': 'content_block_delta', 'index': open_idx,\n"
  "                    'delta': {'type': 'signature_delta', 'signature': thinking_signature()},\n"
  "                })\n"
  "            yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': open_idx})\n")
# 2. emit_synthetic_thinking close
P(NVAC,
  "            'delta': {'type': 'thinking_delta', 'thinking': '[Reasoning not supported by this model; responding directly.]'},\n"
  "        })\n"
  "        yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': thinking_index})\n",
  "            'delta': {'type': 'thinking_delta', 'thinking': '[Reasoning not supported by this model; responding directly.]'},\n"
  "        })\n"
  "        yield _sse('content_block_delta', {\n"
  "            'type': 'content_block_delta', 'index': thinking_index,\n"
  "            'delta': {'type': 'signature_delta', 'signature': thinking_signature()},\n"
  "        })\n"
  "        yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': thinking_index})\n")
# 3. in-stream terminal close (inside try)
P(NVAC,
  "            if open_idx is not None and open_idx not in set(tool_map.values()):\n"
  "                try:\n"
  "                    yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': open_idx})\n"
  "                except Exception:\n"
  "                    pass\n"
  "                open_idx = None\n",
  "            if open_idx is not None and open_idx not in set(tool_map.values()):\n"
  "                try:\n"
  "                    if open_idx == thinking_index:\n"
  "                        yield _sse('content_block_delta', {\n"
  "                            'type': 'content_block_delta', 'index': open_idx,\n"
  "                            'delta': {'type': 'signature_delta', 'signature': thinking_signature()},\n"
  "                        })\n"
  "                    yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': open_idx})\n"
  "                except Exception:\n"
  "                    pass\n"
  "                open_idx = None\n")
# 4. final terminal close
P(NVAC,
  "    if open_idx is not None and open_idx not in set(tool_map.values()):\n"
  "        yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': open_idx})\n"
  "        open_idx = None\n"
  "    # R-02: ensure no tool_use block is left unclosed.\n",
  "    if open_idx is not None and open_idx not in set(tool_map.values()):\n"
  "        if open_idx == thinking_index:\n"
  "            yield _sse('content_block_delta', {\n"
  "                'type': 'content_block_delta', 'index': open_idx,\n"
  "                'delta': {'type': 'signature_delta', 'signature': thinking_signature()},\n"
  "            })\n"
  "        yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': open_idx})\n"
  "        open_idx = None\n"
  "    # R-02: ensure no tool_use block is left unclosed.\n")
# 5. non-stream thinking block
P(NVAC,
  "    _nr = extract_internal_reasoning(msg)\n"
  "    reasoning = _nr['reasoning']\n"
  "    raw_content = _nr['content'] or ''\n"
  "\n"
  "    if reasoning:\n"
  "        content.append({'type': 'thinking', 'thinking': reasoning})\n",
  "    _nr = extract_internal_reasoning(msg)\n"
  "    reasoning = _nr['reasoning']\n"
  "    raw_content = _nr['content'] or ''\n"
  "\n"
  "    if reasoning:\n"
  "        content.append({'type': 'thinking', 'thinking': reasoning, 'signature': thinking_signature()})\n")
# 6. non-stream synthetic thinking insert
P(NVAC,
  "        content.insert(0, {'type': 'thinking', 'thinking': '[Reasoning not supported by this model; responding directly.]'})\n",
  "        content.insert(0, {'type': 'thinking', 'thinking': '[Reasoning not supported by this model; responding directly.]', 'signature': thinking_signature()})\n")

# --------------------------------------------------- nvidia main test path
NVMAIN = "nvidia-python/src/main.py"
P(NVMAIN,
  "    def _close_block(self):\n"
  "        if self.current_block is None:\n"
  "            return []\n"
  "        ev = [self._sse('content_block_stop', {'type': 'content_block_stop', 'index': self.index})]\n"
  "        self.current_block = None\n"
  "        return ev\n",
  "    def _close_block(self):\n"
  "        if self.current_block is None:\n"
  "            return []\n"
  "        ev = []\n"
  "        if self.current_block == 'thinking':\n"
  "            # Anthropic contract: thinking blocks carry a signature. The\n"
  "            # canonical helper lives in common.translations.think_tags;\n"
  "            # this test-path state machine keeps a local constant so it\n"
  "            # stays importable without the common package.\n"
  "            sig = 'd3JhcHBlci10aGlua2luZy12MQ=='  # b64('wrapper-thinking-v1')\n"
  "            ev.append(self._sse('content_block_delta', {\n"
  "                'type': 'content_block_delta', 'index': self.index,\n"
  "                'delta': {'type': 'signature_delta', 'signature': sig},\n"
  "            }))\n"
  "        ev.append(self._sse('content_block_stop', {'type': 'content_block_stop', 'index': self.index}))\n"
  "        self.current_block = None\n"
  "        return ev\n")

# ------------------------------------------------------------------- nous
NOUS = "nous/src/main.py"
P(NOUS,
  "    from common.translations.think_tags import (\n"
  "        ThinkTagStreamFilter as _ThinkTagStreamFilter,\n"
  "        extract_inline_thinking as _extract_inline_thinking,\n"
  "        strip_trailing_think_tags as _strip_think_tags,\n"
  "    )\n",
  "    from common.translations.think_tags import (\n"
  "        ThinkTagStreamFilter as _ThinkTagStreamFilter,\n"
  "        extract_inline_thinking as _extract_inline_thinking,\n"
  "        strip_trailing_think_tags as _strip_think_tags,\n"
  "        thinking_signature as _thinking_signature,\n"
  "    )\n")
# close sites — variant A 12-sp (x2)
P(NOUS,
  '            if self.current_block and self.current_block != "tool_use":\n'
  '                events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": self.index}})\n',
  '            if self.current_block and self.current_block != "tool_use":\n'
  '                if self.current_block == "thinking":\n'
  '                    events.append({"type": "content_block_delta", "data": {\n'
  '                        "type": "content_block_delta", "index": self.index,\n'
  '                        "delta": {"type": "signature_delta", "signature": _thinking_signature()},\n'
  '                    }})\n'
  '                events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": self.index}})\n',
  count=2)
# close sites — variant A 16-sp (x2)
P(NOUS,
  '                if self.current_block and self.current_block != "tool_use":\n'
  '                    events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": self.index}})\n',
  '                if self.current_block and self.current_block != "tool_use":\n'
  '                    if self.current_block == "thinking":\n'
  '                        events.append({"type": "content_block_delta", "data": {\n'
  '                            "type": "content_block_delta", "index": self.index,\n'
  '                            "delta": {"type": "signature_delta", "signature": _thinking_signature()},\n'
  '                        }})\n'
  '                    events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": self.index}})\n',
  count=2)
# close sites — variant B 12-sp (x2)
P(NOUS,
  '            if self.current_block is not None and self.current_block != "tool_use":\n'
  '                events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": self.index}})\n',
  '            if self.current_block is not None and self.current_block != "tool_use":\n'
  '                if self.current_block == "thinking":\n'
  '                    events.append({"type": "content_block_delta", "data": {\n'
  '                        "type": "content_block_delta", "index": self.index,\n'
  '                        "delta": {"type": "signature_delta", "signature": _thinking_signature()},\n'
  '                    }})\n'
  '                events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": self.index}})\n',
  count=2)
# close site — variant B 8-sp (x1, done())
P(NOUS,
  '        if self.current_block is not None and self.current_block != "tool_use":\n'
  '            events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": self.index}})\n',
  '        if self.current_block is not None and self.current_block != "tool_use":\n'
  '            if self.current_block == "thinking":\n'
  '                events.append({"type": "content_block_delta", "data": {\n'
  '                    "type": "content_block_delta", "index": self.index,\n'
  '                    "delta": {"type": "signature_delta", "signature": _thinking_signature()},\n'
  '                }})\n'
  '            events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": self.index}})\n',
  count=1)
# non-stream thinking block
P(NOUS,
  '    if reasoning:\n'
  '        content.append({"type": "thinking", "thinking": reasoning})\n',
  '    if reasoning:\n'
  '        content.append({"type": "thinking", "thinking": reasoning, "signature": _thinking_signature()})\n')

# -------------------------------------------------------------- openrouter
OR = "openrouter/src/main.py"
P(OR,
  "    from common.translations.think_tags import (\n"
  "        ThinkTagStreamFilter as _ThinkTagStreamFilter,\n"
  "        extract_inline_thinking as _extract_inline_thinking,\n"
  "        strip_trailing_think_tags as _strip_think_tags,\n"
  "    )\n",
  "    from common.translations.think_tags import (\n"
  "        ThinkTagStreamFilter as _ThinkTagStreamFilter,\n"
  "        extract_inline_thinking as _extract_inline_thinking,\n"
  "        strip_trailing_think_tags as _strip_think_tags,\n"
  "        thinking_signature as _thinking_signature,\n"
  "    )\n")
# site 1 — _emit_filtered thinking→text transition
P(OR,
  "                if not text_started:\n"
  "                    if block_open and thinking_started:\n"
  "                        yield _sse('content_block_stop', {\n"
  "                            'type': 'content_block_stop', 'index': thinking_index,\n"
  "                        })\n",
  "                if not text_started:\n"
  "                    if block_open and thinking_started:\n"
  "                        yield _sse('content_block_delta', {\n"
  "                            'type': 'content_block_delta', 'index': thinking_index,\n"
  "                            'delta': {'type': 'signature_delta', 'signature': _thinking_signature()},\n"
  "                        })\n"
  "                        yield _sse('content_block_stop', {\n"
  "                            'type': 'content_block_stop', 'index': thinking_index,\n"
  "                        })\n")
# site 2 — tool path elif close
P(OR,
  "                        elif block_open and thinking_started:\n"
  "                            yield _sse('content_block_stop', {\n"
  "                                'type': 'content_block_stop', 'index': thinking_index,\n"
  "                            })\n",
  "                        elif block_open and thinking_started:\n"
  "                            yield _sse('content_block_delta', {\n"
  "                                'type': 'content_block_delta', 'index': thinking_index,\n"
  "                                'delta': {'type': 'signature_delta', 'signature': _thinking_signature()},\n"
  "                            })\n"
  "                            yield _sse('content_block_stop', {\n"
  "                                'type': 'content_block_stop', 'index': thinking_index,\n"
  "                            })\n")
# site 3 — terminal close (8-sp)
P(OR,
  "        if block_open and thinking_started:\n"
  "            yield _sse('content_block_stop', {\n"
  "                'type': 'content_block_stop', 'index': thinking_index,\n"
  "            })\n",
  "        if block_open and thinking_started:\n"
  "            yield _sse('content_block_delta', {\n"
  "                'type': 'content_block_delta', 'index': thinking_index,\n"
  "                'delta': {'type': 'signature_delta', 'signature': _thinking_signature()},\n"
  "            })\n"
  "            yield _sse('content_block_stop', {\n"
  "                'type': 'content_block_stop', 'index': thinking_index,\n"
  "            })\n")
# site 4 — error terminal close (single-line, 12-sp)
P(OR,
  "            if block_open and thinking_started:\n"
  "                yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': thinking_index})\n",
  "            if block_open and thinking_started:\n"
  "                yield _sse('content_block_delta', {\n"
  "                    'type': 'content_block_delta', 'index': thinking_index,\n"
  "                    'delta': {'type': 'signature_delta', 'signature': _thinking_signature()},\n"
  "                })\n"
  "                yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': thinking_index})\n")
# non-stream site 1
P(OR,
  '    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""\n'
  '    if reasoning:\n'
  '        content_blocks.append({"type": "thinking", "thinking": reasoning})\n',
  '    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""\n'
  '    if reasoning:\n'
  '        content_blocks.append({"type": "thinking", "thinking": reasoning, "signature": _thinking_signature()})\n')
# non-stream site 2 (inline extraction)
P(OR,
  '            if _inline:\n'
  '                reasoning = _inline\n'
  '                content_blocks.append({"type": "thinking", "thinking": reasoning})\n',
  '            if _inline:\n'
  '                reasoning = _inline\n'
  '                content_blocks.append({"type": "thinking", "thinking": reasoning, "signature": _thinking_signature()})\n')

# --------------------------------------------------- shared translations
SHARED = "common/translations/shared.py"
P(SHARED,
  "    from .think_tags import extract_inline_thinking, strip_trailing_think_tags\n"
  "except ImportError:  # pragma: no cover - standalone use outside the package\n"
  "    from think_tags import extract_inline_thinking, strip_trailing_think_tags\n",
  "    from .think_tags import extract_inline_thinking, strip_trailing_think_tags, thinking_signature\n"
  "except ImportError:  # pragma: no cover - standalone use outside the package\n"
  "    from think_tags import extract_inline_thinking, strip_trailing_think_tags, thinking_signature\n")
P(SHARED,
  '    reasoning = msg.get("reasoning_content") or msg.get("reasoning")\n'
  '    if reasoning and isinstance(reasoning, str):\n'
  '        content.append({"type": "thinking", "thinking": reasoning})\n',
  '    reasoning = msg.get("reasoning_content") or msg.get("reasoning")\n'
  '    if reasoning and isinstance(reasoning, str):\n'
  '        content.append({"type": "thinking", "thinking": reasoning, "signature": thinking_signature()})\n')
P(SHARED,
  '            if inline_thinking:\n'
  '                reasoning = inline_thinking\n'
  '                content.append({"type": "thinking", "thinking": reasoning})\n',
  '            if inline_thinking:\n'
  '                reasoning = inline_thinking\n'
  '                content.append({"type": "thinking", "thinking": reasoning, "signature": thinking_signature()})\n')

ASTREAM = "common/translations/anthropic_stream.py"
P(ASTREAM,
  "from typing import List, Optional\n",
  "from typing import List, Optional\n\n"
  "try:\n"
  "    from .think_tags import thinking_signature\n"
  "except ImportError:  # pragma: no cover - standalone use outside the package\n"
  "    from think_tags import thinking_signature\n")
P(ASTREAM,
  '        if self.current_block is None or self.current_block == "tool_use":\n'
  '            return []\n'
  '        ev = [self._sse("content_block_stop", {\n'
  '            "type": "content_block_stop",\n'
  '            "index": self.index,\n'
  '        })]\n'
  '        self.current_block = None\n'
  '        return ev\n',
  '        if self.current_block is None or self.current_block == "tool_use":\n'
  '            return []\n'
  '        ev = []\n'
  '        if self.current_block == "thinking":\n'
  '            # Anthropic contract: thinking blocks carry a signature\n'
  '            # (signature_delta immediately before content_block_stop).\n'
  '            ev.append(self._sse("content_block_delta", {\n'
  '                "type": "content_block_delta",\n'
  '                "index": self.index,\n'
  '                "delta": {"type": "signature_delta", "signature": thinking_signature()},\n'
  '            }))\n'
  '        ev.append(self._sse("content_block_stop", {\n'
  '            "type": "content_block_stop",\n'
  '            "index": self.index,\n'
  '        }))\n'
  '        self.current_block = None\n'
  '        return ev\n')


def main():
    backed_up = set()
    failures = []
    for path, old, new, count in PATCHES:
        try:
            with open(path, encoding="utf-8") as f:
                src = f.read()
        except OSError as e:
            failures.append(f"READ FAIL {path}: {e}")
            continue
        n = src.count(old)
        if n != count:
            failures.append(f"COUNT FAIL {path}: expected {count}, found {n} :: {old[:70]!r}")
            continue
        if path not in backed_up:
            shutil.copy2(path, f"{path}.bak-{STAMP}-sig")
            backed_up.add(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(src.replace(old, new))
        print(f"OK  {path} x{count}")
    # think_tags.py: append the helper functions (idempotent)
    tt = f"{BASE}/{TT}"
    with open(tt, encoding="utf-8") as f:
        src = f.read()
    if "def thinking_signature" not in src:
        if tt not in backed_up:
            shutil.copy2(tt, f"{tt}.bak-{STAMP}-sig")
            backed_up.add(tt)
        with open(tt, "a", encoding="utf-8") as f:
            f.write(THINK_TAGS_APPEND)
        print(f"OK  {tt} (appended helpers)")
    else:
        print(f"SKIP {tt} (helpers already present)")
    if failures:
        print("\nFAILURES:")
        for f_ in failures:
            print(" ", f_)
        sys.exit(1)
    print(f"\nAll patches applied. Backups: .bak-{STAMP}-sig")


if __name__ == "__main__":
    main()
