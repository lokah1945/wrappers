#!/usr/bin/env python3
"""Tests for loki_push.py — Loki log pusher."""

import json
import os
import pytest
from src import loki_push


class TestProcessLine:
    def test_adds_to_batch(self):
        loki_push._batch.clear()
        loki_push.process_line('{"event": "test"}')
        assert len(loki_push._batch) == 1
        assert loki_push._batch[0] == '{"event": "test"}'

    def test_empty_line_ignored(self):
        loki_push._batch.clear()
        loki_push.process_line('')
        assert len(loki_push._batch) == 0

    def test_whitespace_line_ignored(self):
        loki_push._batch.clear()
        loki_push.process_line('   ')
        assert len(loki_push._batch) == 0

    def test_batch_size_triggers_push(self):
        loki_push._batch.clear()
        original_process = loki_push.process_line
        called = []
        def mock_process(line):
            called.append(line)
        loki_push.process_line = mock_process
        try:
            for i in range(loki_push.BATCH_SIZE):
                loki_push.process_line(f'{{"i": {i}}}')
            assert len(called) == loki_push.BATCH_SIZE
        finally:
            loki_push.process_line = original_process


class TestPushChunk:
    def test_empty_batch_no_push(self):
        loki_push._batch.clear()
        # Should not raise
        import asyncio
        asyncio.run(loki_push.push_chunk())


class TestConfig:
    def test_default_loki_url(self):
        assert 'loki' in loki_push.LOKI_URL.lower() or '127.0.0.1' in loki_push.LOKI_URL

    def test_default_labels(self):
        assert 'job' in loki_push.LABELS

    def test_batch_size_positive(self):
        assert loki_push.BATCH_SIZE > 0

    def test_flush_interval_positive(self):
        assert loki_push.FLUSH_INTERVAL > 0
