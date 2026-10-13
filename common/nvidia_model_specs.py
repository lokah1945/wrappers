"""Read exact numeric specifications published on a NVIDIA Build model page.

Do not interpret rounded UI labels ('128K', '1M'), model-card prose, sample
max_tokens or another provider's model metadata as hosted endpoint limits.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser

from common.model.metadata import positive_int


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
    values = set()
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
            values.add(value)
    # Conflicting specifications are uncertain, never pick the largest one.
    return values.pop() if len(values) == 1 else None
