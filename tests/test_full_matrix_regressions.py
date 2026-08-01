#!/usr/bin/env python3
"""Regression guards for the five defects found by the 2026-08-01 full-matrix
audit (docs/audits/FULL_MATRIX_AUDIT_2026-08-01.md).

F-1 openrouter chat        max_tokens positive-int + 1M cap (WRAPPER_CONTRACT §4)
F-2 openrouter responses   max_output_tokens/max_tokens 1M cap (§4)
F-3 messages surface       unknown role / orphan tool rejected with 400 (§4)
F-4 nvidia responses       max_output_tokens/max_tokens 1M cap (§4)
F-5 nous/opencode/blackbox X-Request-ID returned on every response (§10)
"""
import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ALL = ('nvidia-python', 'nous', 'opencode', 'blackbox', 'openrouter')


def _src(wname, *parts):
    path = ROOT / wname / 'src'
    for p in parts:
        path = path / p
    return path.read_text()


def _string_consts(src):
    tree = ast.parse(src)
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


# F-1 / F-2 / F-4 — max_tokens / max_output_tokens validation

def test_f1_openrouter_chat_validates_max_tokens():
    src = _src('openrouter', 'main.py')
    i = src.index('async def chat_completions')
    block = src[i:i + 2500]
    assert 'must be a positive integer' in block
    assert 'exceeds maximum allowed value of 1000000' in block


def test_f2_openrouter_responses_caps_max_output_tokens():
    src = _src('openrouter', 'main.py')
    i = src.index('async def responses')
    block = src[i:i + 3000]
    assert 'must be a positive integer' in block
    assert 'exceeds maximum allowed value of 1000000' in block


def test_f4_nvidia_responses_caps_max_output_tokens():
    src = _src('nvidia-python', 'main.py')
    i = src.index('async def responses_api')
    block = src[i:i + 4000]
    assert 'exceeds maximum allowed value of 1000000' in block


# F-3 — messages surface rejects unknown roles / orphan tools

@pytest.mark.parametrize('wname', ALL)
def test_f3_messages_surface_rejects_invalid_role(wname):
    src = _src(wname, 'main.py')
    # every wrapper must contain the invalid-role guard (nvidia keeps its own
    # copy in main.py; all five have it in the messages handler)
    assert 'Invalid role' in src, f'{wname}: messages surface lacks role validation'
    assert 'tool_use_id' in src or 'tool_call_id' in src, \
        f'{wname}: messages surface lacks orphan-tool guard'


# F-5 — X-Request-ID returned on every response

@pytest.mark.parametrize('wname', ('nous', 'opencode', 'blackbox'))
def test_f5_x_request_id_returned_on_responses(wname):
    src = _src(wname, 'main.py')
    assert 'X-Request-ID' in src, f'{wname}: X-Request-ID never set on responses'
    assert 'response.headers["X-Request-ID"]' in src or \
        "response.headers['X-Request-ID']" in src, \
        f'{wname}: X-Request-ID not assigned to the response'


# Matrix harness itself must exist and stay green

def test_matrix_harness_and_evidence_present():
    assert (ROOT / 'tests/e2e_runtime/full_matrix_audit.py').exists()
    assert (ROOT / 'tests/e2e_runtime/render_full_matrix_report.py').exists()
    ev = ROOT / 'docs/audits/FULL_MATRIX_AUDIT_2026-08-01.json'
    assert ev.exists(), 'run python tests/e2e_runtime/full_matrix_audit.py first'
    import json
    data = json.loads(ev.read_text())
    assert data['failed'] == 0, f"{data['failed']} matrix failures recorded"
    assert data['passed'] >= 200, f"only {data['passed']} matrix passes recorded"
