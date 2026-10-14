"""Regression tests for the anti-trace think-tag hygiene (2026-10-15).

Each test mirrors an empirically observed defect from the Claude Code
transcript forensics on the NVIDIA wrapper (9101):
- trailing '</think>' artifacts at message tails
- standalone whitespace text blocks after tool_calls
- mid-sentence thinking fragments from literal tag mentions
- reasoning leaked into visible text
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.translations.think_tags import (  # noqa: E402
    ThinkTagStreamFilter,
    extract_inline_thinking,
    strip_trailing_think_tags,
)


def collect(chunks):
    filt = ThinkTagStreamFilter()
    thinking, text = [], []
    for chunk in chunks:
        for kind, piece in filt.feed(chunk):
            if kind == 'thinking':
                thinking.append(piece)
            else:
                text.append(piece)
    for kind, piece in filt.close():
        if kind == 'thinking':
            thinking.append(piece)
        else:
            text.append(piece)
    return ''.join(thinking), ''.join(text)


def test_inline_extraction_basic():
    thinking, text = collect(['<think>reasoning here</think>the answer'])
    assert thinking == 'reasoning here'
    assert text == 'the answer'


def test_inline_extraction_split_across_chunks():
    # every tag boundary split mid-way through a chunk
    thinking, text = collect(['<th', 'ink>re', 'ason', 'ing</thi', 'nk>ans', 'wer'])
    assert thinking == 'reasoning'
    assert text == 'answer'


def test_thinking_tag_family():
    thinking, text = collect(['<thinking>deep thoughts</thinking>visible'])
    assert thinking == 'deep thoughts'
    assert text == 'visible'


def test_trailing_stray_closer_stripped():
    # transcript L18/L66: '...better.</think>\n' leaked as visible text
    thinking, text = collect(['Let me check the providers directory.</think>\n'])
    assert thinking == ''
    assert text == 'Let me check the providers directory.'


def test_trailing_stray_closer_split_across_chunks():
    thinking, text = collect(['prose.', '</thi', 'nk>', '\n'])
    assert thinking == ''
    assert text == 'prose.'


def test_trailing_stray_multi():
    thinking, text = collect(['answer.</think></think>\n\n'])
    assert text == 'answer.'


def test_midtext_mention_preserved():
    # transcript L90: prose discussing the tags literally
    prose = ('Now I have a clear picture. 1. **The `<thinking>` tag issue**: '
             'the function is never called. This is why `<thinking>` and '
             '`</thinking>` tags appear in responses.')
    thinking, text = collect([prose, ' Final sentence.</think>\n'])
    assert thinking == ''
    assert '<thinking>' in text and '</thinking>' in text
    assert text.startswith('Now I have a clear picture.')
    assert text.endswith('Final sentence.')


def test_midtext_mention_with_text_after_holdback():
    # closer mention followed by whitespace + more text: stays literal
    thinking, text = collect(['a </think> b'])
    assert thinking == ''
    assert text == 'a </think> b'


def test_whitespace_only_dropped_at_detect():
    # NIM trailing newline: filter yields nothing (consumer drop rule)
    filt = ThinkTagStreamFilter()
    out = filt.feed('\n')
    assert out == []
    out = filt.close()
    assert out == []


def test_unclosed_thinking_flushes_as_thinking():
    # DeepSeek-style unclosed reasoning: everything is thinking
    thinking, text = collect(['<think>reasoning that never closes'])
    assert thinking == 'reasoning that never closes'
    assert text == ''


def test_flush_tool_boundary_strips_stray():
    filt = ThinkTagStreamFilter()
    pieces = filt.feed('prose.</think>')
    assert [p for _, p in pieces] == ['prose.']
    flushed = filt.flush_tool_boundary()
    assert flushed == []


def test_extract_inline_thinking_nonstream():
    thinking, text = extract_inline_thinking('<think>reason</think>answer')
    assert thinking == 'reason'
    assert text == 'answer'


def test_extract_inline_thinking_not_anchored():
    thinking, text = extract_inline_thinking('discusses <thinking> tags in prose')
    assert thinking == ''
    assert '<thinking>' in text


def test_extract_inline_thinking_trailing_stray():
    thinking, text = extract_inline_thinking('answer.</think>\n')
    assert thinking == ''
    assert text == 'answer.'


def test_strip_trailing():
    assert strip_trailing_think_tags('answer.</think>\n') == 'answer.'
    assert strip_trailing_think_tags('plain text') == 'plain text'
    assert strip_trailing_think_tags('') == ''


def test_leading_whitespace_before_opener():
    thinking, text = collect(['\n\n<think>reason</think>answer'])
    assert thinking == 'reason'
    assert text == 'answer'


def test_reopen_not_triggered_midstream():
    # after text mode engaged, later opener mentions stay literal
    chunks = ['first ', 'mention of <think> here', ' and </think> there']
    thinking, text = collect(chunks)
    assert thinking == ''
    assert '<think>' in text and '</think>' in text


if __name__ == '__main__':
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f'PASS {name}')
            except AssertionError as exc:
                fails += 1
                print(f'FAIL {name}: {exc}')
    print('ALL PASS' if not fails else f'{fails} FAILURES')
    sys.exit(1 if fails else 0)
