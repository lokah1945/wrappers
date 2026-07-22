#!/usr/bin/env python3
"""Tests for alert_history.py — alert event historian."""

import json
import os
import tempfile
import pytest
from src import alert_history


class TestClassify:
    def test_exhaustion(self):
        ev = {'msg': 'all keys failed for model X'}
        cls = alert_history.classify(ev)
        assert cls['kind'] == 'exhaustion'
        assert cls['severity'] == 'critical'

    def test_rate_limit(self):
        ev = {'msg': 'Got 429 rate limit error'}
        cls = alert_history.classify(ev)
        assert cls['kind'] == 'rate_limit'
        assert cls['severity'] == 'warn'

    def test_upstream_5xx(self):
        ev = {'msg': 'Upstream returned 500'}
        cls = alert_history.classify(ev)
        assert cls['kind'] == 'upstream_5xx'

    def test_model_unavailable(self):
        ev = {'msg': 'Model not found'}
        cls = alert_history.classify(ev)
        assert cls['kind'] == 'model_unavailable'

    def test_pacing(self):
        ev = {'msg': 'pacing enabled'}
        cls = alert_history.classify(ev)
        assert cls['kind'] == 'pacing'
        assert cls['severity'] == 'info'

    def test_key_disabled(self):
        ev = {'msg': 'key_disabled'}
        cls = alert_history.classify(ev)
        assert cls['kind'] == 'key_disabled'

    def test_no_match(self):
        ev = {'msg': 'Some random message'}
        assert alert_history.classify(ev) is None


class TestShouldEmit:
    def test_first_emit(self):
        alert_history._dedupe.clear()
        assert alert_history.should_emit('rate_limit', 'model_x') is True

    def test_dedupe_within_window(self):
        alert_history._dedupe.clear()
        alert_history.should_emit('rate_limit', 'model_x')
        assert alert_history.should_emit('rate_limit', 'model_x') is False

    def test_dedupe_different_model(self):
        alert_history._dedupe.clear()
        alert_history.should_emit('rate_limit', 'model_x')
        assert alert_history.should_emit('rate_limit', 'model_y') is True

    def test_dedupe_different_kind(self):
        alert_history._dedupe.clear()
        alert_history.should_emit('rate_limit', 'model_x')
        assert alert_history.should_emit('upstream_5xx', 'model_x') is True


class TestEmitAlert:
    def test_full_record(self):
        ev = {
            'ts': '2024-01-01T00:00:00Z',
            'msg': '429 rate limit',
            'model': 'nvidia/llama',
            'key_label': 'key1',
            'attempt': 2,
            'status': 429,
            'client_ip': '1.2.3.4',
            'scope': 'global',
            'in_flight': 5,
            'scheme': 'https',
            'rpm': 40,
            'latency_ms': 150,
        }
        cls = {'kind': 'rate_limit', 'severity': 'warn'}
        rec = alert_history.emit_alert(ev, cls)
        assert rec['kind'] == 'rate_limit'
        assert rec['model'] == 'nvidia/llama'
        assert rec['key_label'] == 'key1'
        assert rec['attempt'] == 2
        assert rec['status'] == 429
        assert rec['client_ip'] == '1.2.3.4'

    def test_null_fields_removed(self):
        ev = {'msg': 'test', 'model': None, 'key_label': 'key1'}
        cls = {'kind': 'rate_limit', 'severity': 'warn'}
        rec = alert_history.emit_alert(ev, cls)
        assert 'model' not in rec
        assert rec['key_label'] == 'key1'


class TestProcessLine:
    def test_valid_json(self, tmp_path):
        output = tmp_path / 'alert-history.jsonl'
        alert_history.OUTPUT = str(output)
        alert_history._dedupe.clear()
        ev = {'msg': '429 rate limit', 'model': 'nvidia/llama', 'ts': '2024-01-01T00:00:00Z'}
        alert_history.process_line(json.dumps(ev))
        assert output.exists()
        lines = output.read_text().strip().split('\n')
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec['kind'] == 'rate_limit'

    def test_empty_line(self, tmp_path):
        output = tmp_path / 'alert-history.jsonl'
        alert_history.OUTPUT = str(output)
        alert_history.process_line('')
        assert not output.exists()

    def test_non_json_fallback(self, tmp_path):
        output = tmp_path / 'alert-history.jsonl'
        alert_history.OUTPUT = str(output)
        alert_history._dedupe.clear()
        alert_history.process_line('429 rate limit error')
        assert output.exists()
        rec = json.loads(output.read_text().strip())
        assert rec['kind'] == 'rate_limit'
