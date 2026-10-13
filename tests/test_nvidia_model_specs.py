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


def test_conflicting_selected_model_capacity_is_unknown():
    document = model_record(1048576) + model_record(131072, row='59')
    assert parse_build_context(page(document), MODEL) is None


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

