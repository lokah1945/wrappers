"""Bounded recovery from explicit upstream token-limit rejections.

No token estimates, model substitution, history deletion or stream replay.
Only an explicit output ceiling can be reduced, before any successful stream
is returned. A prompt that does not fit needs caller-directed compaction.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from dataclasses import asdict, dataclass

from common.model.metadata import positive_int

logger = logging.getLogger(__name__)
# Reject prefixes of abbreviated/decimal limits ("1M", "8.5k") and malformed
# comma groups. A provider must supply an exact integer before we change a cap.
_NUMBER = r'(?<![0-9A-Za-z_.])([0-9]{1,18}(?:,[0-9]{3}){0,5})(?![0-9A-Za-z_]|\.[0-9]|,[0-9])'
_OUTPUT_FIELDS = ('max_tokens', 'max_completion_tokens', 'max_output_tokens')


@dataclass(frozen=True)
class ContextLimitEvidence:
    context_window: int | None = None
    input_tokens: int | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class ContextRecoveryPlan:
    evidence: ContextLimitEvidence | None
    retry_payload: dict | None = None
    action: str = 'not_context_error'


def _number(text, pattern):
    match = re.search(pattern, text, re.I)
    return int(match.group(1).replace(',', '')) if match else None


def parse_context_error(status, error):
    if status not in (400, 413, 422):
        return None
    if isinstance(error, (bytes, str)):
        try:
            error = json.loads(error)
        except (ValueError, TypeError):
            error = {'message': str(error)}
    if not isinstance(error, dict):
        return None
    validation = error.get('detail')
    if isinstance(validation, list):
        caps = []
        for item in validation:
            if not isinstance(item, dict) or item.get('type') != 'less_than_equal':
                continue
            location = item.get('loc')
            if (isinstance(location, list) and location and location[-1] in _OUTPUT_FIELDS
                    and all(part in ('body', *_OUTPUT_FIELDS) for part in location)):
                ctx = item.get('ctx')
                cap = positive_int(ctx.get('le')) if isinstance(ctx, dict) else None
                if cap is not None:
                    caps.append(cap)
        if caps:
            return ContextLimitEvidence(max_output_tokens=min(caps))
    detail = error.get('error', error)
    if not isinstance(detail, dict):
        detail = error
    # Read error text, never a serialized echoed request (e.g. validation input).
    message = detail.get('message') or detail.get('detail') or ''
    text = message[:16000] if isinstance(message, str) else ''
    code = str(detail.get('code') or detail.get('type') or '').lower()
    metadata = detail.get('metadata')
    if isinstance(metadata, dict) and isinstance(metadata.get('raw'), str):
        nested = parse_context_error(status, metadata['raw'][:16000])
        if nested is not None:
            return nested
    context = _number(text, r'(?:maximum|max)\s+context\s+(?:length|window)(?:\s+(?:is|of))?\s*[:=]?\s*' + _NUMBER)
    input_tokens = _number(text, _NUMBER + r'\s+(?:tokens?\s+)?(?:in (?:the )?(?:messages|prompt|input)|input tokens|prompt tokens)')
    if input_tokens is None:
        input_tokens = _number(text, r'(?:input|prompt|messages)\s+(?:token\s+)?(?:count|length|tokens)\s*(?:is|are|:|=)?\s*' + _NUMBER)
    too_long = re.search(r'prompt is too long:\s*' + _NUMBER + r'\s*tokens?\s*>\s*' + _NUMBER, text, re.I)
    if too_long:
        input_tokens, context = [int(value.replace(',', '')) for value in too_long.groups()]
    sum_error = re.search(r'(?:input length|input tokens).*?max_tokens.*?context (?:limit|length).*?:\s*'
                          + _NUMBER + r'\s*\+\s*' + _NUMBER + r'\s*>\s*' + _NUMBER, text, re.I)
    if sum_error:
        input_tokens = int(sum_error.group(1).replace(',', ''))
        context = int(sum_error.group(3).replace(',', ''))
    output = _number(text, r'(?:max_tokens|max_completion_tokens|max_output_tokens)[\s\'"`]*'
                          r'(?:(?:must be|must have|should be)\s*'
                          r'(?:less than or equal to|at most|<=|≤)|'
                          r'is limited to|cannot exceed|must not exceed)\s*' + _NUMBER)
    if output is None:
        output = _number(text, r'maximum (?:output|completion) (?:tokens|length)(?:\s+(?:is|of))?\s*[:=]?\s*' + _NUMBER)
    # Structured provider error facts, only after a definite context code/text.
    definite = (context is not None or output is not None or code in
                ('context_length_exceeded', 'max_tokens_exceeded', 'prompt_too_long') or
                bool(re.search(r'context (?:length|window|limit).*exceed|prompt is too long', text, re.I)))
    if not definite:
        return None
    context = context or positive_int(detail.get('max_context_length')) or positive_int(detail.get('context_length'))
    input_tokens = input_tokens or positive_int(detail.get('input_tokens')) or positive_int(detail.get('prompt_tokens'))
    output = output or positive_int(detail.get('max_output_tokens'))
    return ContextLimitEvidence(context, input_tokens, output)


def plan_context_recovery(payload, status, error, *, attempted=False):
    evidence = parse_context_error(status, error)
    if evidence is None:
        return ContextRecoveryPlan(None)
    if attempted:
        return ContextRecoveryPlan(evidence, action='retry_exhausted')
    if evidence.context_window and evidence.input_tokens is not None:
        room = evidence.context_window - evidence.input_tokens
        if room <= 0:
            return ContextRecoveryPlan(evidence, action='compact_input')
    else:
        room = None
    fields = [key for key in _OUTPUT_FIELDS if key in payload]
    if (len(fields) != 1 or not isinstance(payload[fields[0]], int)
            or positive_int(payload[fields[0]]) is None):
        return ContextRecoveryPlan(evidence, action='set_explicit_output_limit')
    candidates = [v for v in (room, evidence.max_output_tokens) if v is not None and v > 0]
    if not candidates:
        return ContextRecoveryPlan(evidence, action='compact_input_or_reduce_output')
    field = fields[0]
    cap = min(candidates)
    if cap >= payload[field]:
        return ContextRecoveryPlan(evidence, action='compact_input_or_reduce_output')
    # Preserve reasoning/minimum-output semantics instead of silently spending
    # the entire answer budget on thinking or violating a provider constraint.
    minimum = max([positive_int(payload.get(k)) or 0 for k in
                   ('min_tokens', 'min_new_tokens', 'reasoning_budget', 'max_thinking_tokens')])
    for name in ('thinking', 'reasoning'):
        value = payload.get(name)
        if isinstance(value, dict):
            minimum = max(minimum, positive_int(value.get('budget_tokens')) or 0,
                          positive_int(value.get('max_tokens')) or 0)
    # These fields are forwarded by NVIDIA/NIM reasoning-control translators.
    # Do not lower output beneath an explicit native reasoning allocation.
    template = payload.get('chat_template_kwargs')
    if isinstance(template, dict):
        minimum = max(minimum, *(positive_int(template.get(k)) or 0 for k in
                      ('reasoning_budget', 'max_thinking_tokens', 'thinking_budget')))
    if minimum >= cap:
        return ContextRecoveryPlan(evidence, action='adjust_reasoning_budget')
    result = copy.deepcopy(payload)
    result[field] = cap
    return ContextRecoveryPlan(evidence, result, 'reduced_output')


class ContextRecoveryState:
    def __init__(self):
        self.attempted = False
        self.headers = {}
        self.output_field = None
        self.output_tokens = None


def context_response_headers(response):
    headers = getattr(response, 'upstream_headers', getattr(response, 'headers', {}))
    inherited = {key: value for key, value in headers.items()
                 if key.lower().startswith('x-context-')}
    return {**inherited, **getattr(response, '_context_recovery_headers', {})}


class ContextJSON(dict):
    """Carry local transport annotations without modifying native JSON data."""
    def __init__(self, payload, response):
        super().__init__(payload)
        self._context_recovery_headers = context_response_headers(response)


class context_request:
    """Awaitable + async context manager, matching aiohttp ownership semantics."""
    def __init__(self, send, payload, *, state=None, store=None, credential='', endpoint=''):
        self.send, self.payload = send, payload
        self.state = state or ContextRecoveryState()
        self.store, self.credential, self.endpoint = store, credential, endpoint
        self.response = None

    async def _run(self):
        payload = self.payload
        # A repaired request may then hit a quota/auth failure and rotate keys.
        # Keep its reduced ceiling across those attempts, with the same global
        # one-repair bound, without replaying or replacing any input fields.
        field, cap = self.state.output_field, self.state.output_tokens
        if (isinstance(payload, dict) and field in payload and cap is not None
                and isinstance(payload[field], int) and not isinstance(payload[field], bool)
                and payload[field] > cap):
            payload = {**payload, field: cap}
        while True:
            response = await self.send(payload)
            try:
                if not isinstance(payload, dict) or response.status not in (400, 413, 422):
                    response._context_recovery_headers = dict(self.state.headers)
                    return response
                error = await response.text()
                plan = plan_context_recovery(payload, response.status, error, attempted=self.state.attempted)
                if plan.evidence is None:
                    response._context_recovery_headers = dict(self.state.headers)
                    return response
                self.state.headers['X-Context-Recovery'] = plan.action
                self.state.headers['X-Context-Recovery-Hint'] = (
                    'Output ceiling reduced; input preserved.' if plan.retry_payload else
                    'Compact input or lower output/thinking budget; original input was preserved.')
                if self.store is not None:
                    try:
                        await asyncio.to_thread(self.store.record_context_limit,
                            payload.get('model', ''), self.credential, self.endpoint, asdict(plan.evidence))
                    except Exception as exc:
                        logger.warning('Could not persist context evidence: %s', type(exc).__name__)
                if plan.retry_payload is None:
                    response._context_recovery_headers = dict(self.state.headers)
                    return response
                self.state.attempted = True
                payload = plan.retry_payload
                field = next(key for key in _OUTPUT_FIELDS if key in payload)
                output = payload[field]
                self.state.output_field, self.state.output_tokens = field, output
                self.state.headers['X-Context-Output-Tokens'] = str(output)
            except BaseException:
                response.release()
                raise
            response.release()

    def __await__(self):
        return self._run().__await__()

    async def __aenter__(self):
        self.response = await self._run()
        return self.response

    async def __aexit__(self, *exc):
        if self.response is not None:
            self.response.release()
