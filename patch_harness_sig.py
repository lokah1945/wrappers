#!/usr/bin/env python3
"""Patch the AST test harness to inject _thinking_signature into extracted
namespaces (NOUS/OR functions now reference it after the signature fix)."""
import shutil

p = "/root/wrapper/tests/test_active_native_passthrough.py"
shutil.copy2(p, p + ".bak-sig")
src = open(p).read()

old = ("from common.translations.think_tags import (\n"
       "    ThinkTagStreamFilter, extract_inline_thinking, strip_trailing_think_tags,\n"
       ")")
new = ("from common.translations.think_tags import (\n"
       "    ThinkTagStreamFilter, extract_inline_thinking, strip_trailing_think_tags,\n"
       "    thinking_signature,\n"
       ")")
assert src.count(old) == 1, f"import count {src.count(old)}"
src = src.replace(old, new)

old2 = "    namespace.setdefault('_ThinkTagStreamFilter', ThinkTagStreamFilter)"
new2 = ("    namespace.setdefault('_ThinkTagStreamFilter', ThinkTagStreamFilter)\n"
        "    namespace.setdefault('_thinking_signature', thinking_signature)")
assert src.count(old2) == 1, f"setdefault count {src.count(old2)}"
src = src.replace(old2, new2)

open(p, "w").write(src)
print("harness patched OK")
