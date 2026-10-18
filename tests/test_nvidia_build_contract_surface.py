from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'nvidia-python'))
sys.path.insert(0, str(ROOT / 'nvidia-python' / 'src'))

from common.model.contracts import ErrorState  # noqa: E402
from common.model.validation import validate_observation  # noqa: E402
from common.model_state import ModelStateStore  # noqa: E402
from common.nvidia_build_catalog import BUILD_MODEL_IDS, get_build_contract  # noqa: E402
from src import main as main_module  # noqa: E402
from src.main import Server, _contract_endpoint_is_current, _probe_spec  # noqa: E402


def test_build_surface_is_reconciled_and_downloadable_rows_are_candidates():
    assert len(BUILD_MODEL_IDS) == 99
    row = get_build_contract('qwen/qwen-image')
    assert row is not None
    assert row['downloadable'] is True
    assert row['contract']['verification'] == 'deployment_required'
    assert _probe_spec('qwen/qwen-image')['state_hint'] == 'deployment_required'


def test_native_contract_states_are_valid_observations():
    for state in ('native_protocol', 'deployment_required', 'contract_unverified'):
        assert state in {item.value for item in ErrorState}
        observation = validate_observation(
            'vendor/model', 'account-scope', state, 'CONTRACT', 'detail', '/v1/native/vendor/model'
        )
        assert observation['state'] == state


def test_custom_build_probes_use_documented_route_and_payload():
    ocr = _probe_spec('nvidia/nemotron-ocr-v2')
    assert ocr['endpoint'] == '/v1/cv/nvidia/nemotron-ocr-v2'
    assert ocr['body']['input'][0]['type'] == 'image_url'
    assert '/v1/infer' not in ocr['url']

    cosmos = _probe_spec('nvidia/cosmos3-nano')
    assert cosmos['endpoint'] == '/v1/cosmos/nvidia/cosmos3-nano'
    assert cosmos['body'] == {'messages': [{'role': 'user', 'content': ''}]}

    grpc = _probe_spec('nvidia/body-pose')
    assert grpc['deferred'] is True
    assert grpc['state_hint'] == 'native_protocol'
    assert grpc['endpoint'] == 'grpc://grpc.nvcf.nvidia.com:443'


def test_deployed_nim_uses_its_documented_container_path(monkeypatch):
    monkeypatch.setenv('NVIDIA_NIM_BASE_URL', 'http://nim.example:8000')
    server = object.__new__(Server)
    target, row, error = server._native_contract_target('nvidia/nemotron-ocr-v2')
    assert error is None
    assert target == 'http://nim.example:8000/v1/ocr'
    assert row['contract']['deployment_path'] == '/v1/ocr'

    target, _, error = server._native_contract_target('qwen/qwen-image')
    assert error is None
    assert target == 'http://nim.example:8000/v1/infer'


def test_current_endpoint_accepts_hosted_or_deployed_contract_but_not_generic_route():
    assert _contract_endpoint_is_current(
        'nvidia/nemotron-ocr-v2', '/v1/cv/nvidia/nemotron-ocr-v2'
    )
    assert _contract_endpoint_is_current('nvidia/nemotron-ocr-v2', '/v1/ocr')
    assert not _contract_endpoint_is_current('nvidia/nemotron-ocr-v2', '/v1/chat/completions')


def test_epoch_filter_happens_before_effective_account_merge(tmp_path, monkeypatch):
    store = ModelStateStore('nvidia', tmp_path / 'state.db')
    store.record_status(
        'meta/llama-3.2-11b-vision-instruct', 'old-account', 'available', 200,
        'OK', endpoint='/v1/chat/completions',
    )
    epoch = time.time()
    store.record_status(
        'meta/llama-3.2-11b-vision-instruct', 'new-account', 'wrong_route', 404,
        'UPSTREAM_ROUTE_NOT_FOUND', endpoint='/v1/meta/llama-3.2-11b-vision-instruct',
    )
    monkeypatch.setattr(main_module, '_BUILD_AUDIT_EPOCH', epoch)
    server = object.__new__(Server)
    server.model_state = store
    view = server._model_status_view({
        'meta/llama-3.2-11b-vision-instruct': {
            'ok': True, 'availability_state': 'available',
            'http_status': 200, 'endpoint': '/v1/chat/completions',
        }
    })
    state = view['meta/llama-3.2-11b-vision-instruct']
    assert state['availability_state'] == 'wrong_route'
    assert state['endpoint'] == '/v1/meta/llama-3.2-11b-vision-instruct'


def test_stale_build_metric_is_removed_before_audit_observation_exists(tmp_path, monkeypatch):
    store = ModelStateStore('nvidia', tmp_path / 'state.db')
    epoch = time.time()
    store.record_status(
        'nvidia/body-pose', 'old-account', 'available', 200,
        'OK', endpoint='/v1/chat/completions',
    )
    monkeypatch.setattr(main_module, '_BUILD_AUDIT_EPOCH', epoch)
    server = object.__new__(Server)
    server.model_state = store
    view = server._model_status_view({
        'nvidia/body-pose': {
            'ok': True, 'availability_state': 'available',
            'http_status': 200, 'endpoint': '/v1/chat/completions',
        }
    })
    assert 'nvidia/body-pose' not in view
