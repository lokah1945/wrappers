"""The published capacity must belong to the requested NVIDIA Build model."""
import json

import pytest

from common.nvidia_model_specs import build_model_url, parse_build_context


MODEL = 'nvidia/nemotron-3-ultra-550b-a55b'


def model_record(context, model=MODEL, row='58'):
    # Shape observed in NVIDIA Build's model React component. The namespace
    # value is an internal identifier, distinct from the public organization.
    spec = {
        'namespace': 'internal-namespace',
        'artifactName': model.split('/')[1],
        'attributes': {'specifications': {'contextLength': context}},
    }
    return row + ':' + json.dumps(['$', '$L5a', None, {'spec': spec}]) + '\n'


def page(document, model=MODEL, split_at=None):
    chunks = [document] if split_at is None else [document[:split_at], document[split_at:]]
    scripts = ''.join('<script>self.__next_f.push(' + json.dumps([1, chunk]) + ')</script>'
                      for chunk in chunks)
    return '<link rel="canonical" href="' + build_model_url(model) + '">' + scripts


def test_exact_selected_component_capacity_across_flight_chunks():
    document = '1:"$Sreact.fragment"\n23:I[292319,[],"default",1]\n' + model_record(1048576)
    assert parse_build_context(page(document, split_at=91), MODEL) == 1048576


@pytest.mark.parametrize('value', ['1M', True, 0, -1, 1048576.5, None])
def test_rounded_or_invalid_capacity_is_unknown(value):
    assert parse_build_context(page(model_record(value)), MODEL) is None


def test_same_artifact_on_different_organization_is_not_selected_model():
    assert parse_build_context(page(model_record(1048576), model='other/' + MODEL.split('/')[1]), MODEL) is None


def test_conflicting_canonical_metadata_is_rejected():
    document = page(model_record(1048576))
    document += '<meta property="og:url" content="https://build.nvidia.com/other/model/build">'
    assert parse_build_context(document, MODEL) is None


def test_related_model_and_unowned_specifications_cannot_supply_capacity():
    unowned = '3:' + json.dumps({'specifications': {'contextLength': 1048576}}) + '\n'
    related = model_record(1048576, model='nvidia/related-model')
    nested = '4:' + json.dumps(['$', '$L1', None, {
        'relatedModel': {'specifications': {'contextLength': 1048576}},
    }]) + '\n'
    assert parse_build_context(page(unowned + related + nested), MODEL) is None


def test_related_model_capacity_does_not_conflict_with_selected_component():
    document = model_record(131072, model='nvidia/related-model') + model_record(1048576, row='59')
    assert parse_build_context(page(document), MODEL) == 1048576


def test_conflicting_selected_model_capacity_returns_max():
    document = model_record(1048576) + model_record(131072, row='59')
    assert parse_build_context(page(document), MODEL) == 1048576


def test_duplicate_identical_selected_specifications_are_consistent():
    document = model_record(1048576) + model_record(1048576, row='59')
    assert parse_build_context(page(document), MODEL) == 1048576


def test_flight_length_delimited_text_cannot_inject_model_specifications():
    prose = 'Example with Unicode π:\n' + model_record(131072)
    text_row = '2:T' + format(len(prose.encode('utf-8')), 'x') + ',' + prose
    assert parse_build_context(page(text_row), MODEL) is None
    assert parse_build_context(page(text_row + model_record(1048576)), MODEL) == 1048576


def test_malformed_flight_length_cannot_expose_embedded_model_metadata():
    document = '2:Tfffffff,example\n' + model_record(1048576)
    assert parse_build_context(page(document), MODEL) is None


def test_model_path_requires_exact_single_org_and_model():
    for model in ('../model', 'nvidia/..', 'nvidia/model/elsewhere', 'https://elsewhere/model'):
        assert build_model_url(model) is None


def test_prose_context_override_spec():
    # Prose says "1M tokens", spec says 262144 -> should return 1048576
    spec = {
        'namespace': 'internal-namespace',
        'artifactName': 'mistral-large-4-1t-a52b',
        'attributes': {
            'specifications': {'contextLength': 262144},
            'description': 'Text and images share a combined input/output context window of up to 1M tokens',
        },
    }
    record = '58:' + json.dumps(['$', '$L5a', None, {'spec': spec}]) + '\n'
    document = page(record, model='mistralai/mistral-large-4-1t-a52b')
    assert parse_build_context(document, 'mistralai/mistral-large-4-1t-a52b') == 1048576


def test_prose_context_m_parsing():
    # "1M tokens" -> 1048576 (1M = 1,000,000 approximately)
    spec = {
        'namespace': 'internal-namespace',
        'artifactName': 'test-model',
        'attributes': {
            'specifications': {'contextLength': 131072},
            'specsDescription': 'Combined input and output context length is 1M tokens',
        },
    }
    record = '58:' + json.dumps(['$', '$L5a', None, {'spec': spec}]) + '\n'
    document = page(record, model='org/test-model')
    assert parse_build_context(document, 'org/test-model') == 1048576


def test_prose_context_exact_number_parsing():
    # "1,048,576 tokens" -> 1048576
    spec = {
        'namespace': 'internal-namespace',
        'artifactName': 'test-model',
        'attributes': {
            'specifications': {'contextLength': 131072},
            'longDescription': 'The model supports a context window of 1,048,576 tokens for input and output',
        },
    }
    record = '58:' + json.dumps(['$', '$L5a', None, {'spec': spec}]) + '\n'
    document = page(record, model='org/test-model')
    assert parse_build_context(document, 'org/test-model') == 1048576


def test_prose_context_k_parsing_rounded():
    # "128K tokens" -> 131072 (flagged as rounded but still usable)
    spec = {
        'namespace': 'internal-namespace',
        'artifactName': 'test-model',
        'attributes': {
            'specifications': {'contextLength': 65536},
            'marketingDescription': 'Up to 128K tokens context window',
        },
    }
    record = '58:' + json.dumps(['$', '$L5a', None, {'spec': spec}]) + '\n'
    document = page(record, model='org/test-model')
    assert parse_build_context(document, 'org/test-model') == 131072


def test_prose_conflict_logging(caplog):
    # Verify warning is logged when prose > spec * 2
    import logging
    caplog.set_level(logging.WARNING)
    spec = {
        'namespace': 'internal-namespace',
        'artifactName': 'test-model',
        'attributes': {
            'specifications': {'contextLength': 262144},
            'description': 'Context window of up to 1M tokens',
        },
    }
    record = '58:' + json.dumps(['$', '$L5a', None, {'spec': spec}]) + '\n'
    document = page(record, model='org/test-model')
    result = parse_build_context(document, 'org/test-model')
    assert result == 1048576
    assert any('conflicts with prose' in r.message for r in caplog.records)

