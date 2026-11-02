"""Read exact numeric specifications published on a NVIDIA Build model page.

Do not interpret rounded UI labels ('128K', '1M'), model-card prose, sample
max_tokens or another provider's model metadata as hosted endpoint limits.
"""
from __future__ import annotations

import json
import logging
import re
from html.parser import HTMLParser

from common.model.metadata import positive_int

logger = logging.getLogger(__name__)


def build_model_url(model_id):
    if not isinstance(model_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', model_id):
        return None
    if any(part in ('.', '..') for part in model_id.split('/')):
        return None
    return f'https://build.nvidia.com/{model_id}/build'


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.canonical = set()
        self.scripts = []
        self._script = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('property') == 'og:url':
            self.canonical.add(attrs.get('content', '').rstrip('/'))
        if tag == 'link' and attrs.get('rel') == 'canonical':
            self.canonical.add(attrs.get('href', '').rstrip('/'))
        if tag == 'script':
            self._script = []

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self._script is not None:
            self.scripts.append(''.join(self._script))
            self._script = None


def _flight_records(document):
    """Decode JSON rows without mistaking length-delimited prose for a row.

    Flight text rows are UTF-8 byte counted, can contain newlines and need not
    end with a newline. A regex search for JSON anywhere in the stream would
    also read examples and related-model descriptions as model metadata.
    """
    try:
        stream = document.encode('utf-8')
    except UnicodeError:
        return
    position = 0
    row_header = re.compile(rb'[0-9a-f]*:')
    sized_header = re.compile(rb'([0-9a-f]+),')
    sized_tags = b'TAOoUSsLlGgMmV'
    while position < len(stream):
        header = row_header.match(stream, position)
        if header is None:
            return  # Unknown framing must not turn text into metadata.
        position = header.end()
        if position >= len(stream):
            return
        if stream[position] in sized_tags:
            sized = sized_header.match(stream, position + 1)
            if sized is None:
                return
            position = sized.end() + int(sized.group(1), 16)
            if position > len(stream):
                return
            continue
        end = stream.find(b'\n', position)
        if end < 0:
            end = len(stream)
        payload = stream[position:end]
        position = end + 1
        if not payload or payload[0] not in b'[{':
            continue  # Imports, hints, references and other Flight row tags.
        try:
            yield json.loads(payload)
        except (ValueError, UnicodeDecodeError, RecursionError):
            continue


_PROSE_CONTEXT_PATTERNS = [
    # "1M tokens", "1 M tokens", "1m tokens" -> 1048576 (2^20, binary megabyte)
    (re.compile(r'(?i)(\d+(?:\.\d+)?)\s*M\s*tokens?'), 1_048_576),
    # "1,048,576 tokens", "1048576 tokens" near "context window"
    (re.compile(r'(?i)(\d{1,3}(?:,\d{3})+|\d{4,})\s*tokens?'), 1),
    # "128K tokens", "128 K tokens" -> 131072 (2^17 = 128 * 1024)
    (re.compile(r'(?i)(\d+(?:\.\d+)?)\s*K\s*tokens?'), 1_024),
]


def _extract_prose_context(attributes: dict) -> list[int]:
    """Extract context window mentions from prose description fields.

    Scans known text attributes for patterns like "1M tokens", "128K tokens",
    "1,048,576 tokens" near "context window" phrasing.
    """
    text_fields = [
        attributes.get('description'),
        attributes.get('specsDescription'),
        attributes.get('longDescription'),
        attributes.get('shortDescription'),
        attributes.get('marketingDescription'),
    ]
    values = []
    for text in text_fields:
        if not isinstance(text, str):
            continue
        for pattern, multiplier in _PROSE_CONTEXT_PATTERNS:
            for match in pattern.finditer(text):
                try:
                    raw = match.group(1).replace(',', '')
                    value = int(float(raw) * multiplier)
                    if value > 0:
                        values.append(value)
                except (ValueError, IndexError):
                    continue
    return values


def parse_build_context(html, model_id):
    url = build_model_url(model_id)
    if not url:
        return None
    page = _Page()
    page.feed(html)
    accepted = {url, url.removesuffix('/build')}
    if not page.canonical or not page.canonical <= accepted:
        return None  # redirect, error page, or metadata for another model
    decoder = json.JSONDecoder()
    chunks = []
    for script in page.scripts:
        for match in re.finditer(r'self\.__next_f\.push\(', script):
            try:
                data, _ = decoder.raw_decode(script, match.end())
            except (ValueError, RecursionError):
                continue
            if isinstance(data, list) and len(data) == 2 and data[0] == 1 and isinstance(data[1], str):
                chunks.append(data[1])
    document = ''.join(chunks)
    spec_values = set()
    prose_values = set()
    artifact = model_id.split('/')[1]
    for record in _flight_records(document):
        # NVIDIA's model component owns a spec object. In particular, do not
        # recursively scan specifications from related models or API examples.
        if not (isinstance(record, list) and len(record) == 4
                and record[0] == '$' and isinstance(record[3], dict)):
            continue
        spec = record[3].get('spec')
        if not isinstance(spec, dict) or spec.get('artifactName') != artifact:
            continue
        attributes = spec.get('attributes')
        if not isinstance(attributes, dict):
            continue
        specs = attributes.get('specifications')
        value = positive_int(specs.get('contextLength')) if isinstance(specs, dict) else None
        if value:
            spec_values.add(value)
        # Extract prose context mentions from description fields
        prose = _extract_prose_context(attributes)
        for p in prose:
            prose_values.add(p)
    # Resolve conflicts between spec and prose values
    # If prose indicates significantly larger context (2x+), prefer prose
    # as live API probes have proven prose correct for mistral-large-4
    all_values = set()
    if spec_values:
        all_values.update(spec_values)
    if prose_values:
        all_values.update(prose_values)
    if not all_values:
        return None
    if len(all_values) == 1:
        return all_values.pop()
    # Multiple values found - apply conflict resolution
    max_spec = max(spec_values) if spec_values else 0
    max_prose = max(prose_values) if prose_values else 0
    if max_prose > max_spec * 2:
        logger.warning(
            'build page spec contextLength=%s conflicts with prose=%s for %s; '
            'preferring prose value (live API proven correct for similar cases)',
            max_spec, max_prose, model_id
        )
        return max_prose
    # Otherwise return the largest value (don't discard all data)
    return max(all_values)
