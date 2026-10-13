"""Provider metadata/cache regressions; temporary SQLite and fake fetch only."""
import asyncio
import copy
import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.model import LocalModelRegistry, LimitProfile, ModelProfile
from common.model.metadata import annotate_catalog, context_metadata, discovery_limits, extract_limits, merge_context_metadata
from common.model_catalog import CachedModelCatalog
from common.model_state import ModelStateStore
from common.wrapper_scope import select_wrappers

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = select_wrappers(include_registry=False)


def test_context_never_comes_from_family_or_output_default():
    for value in ({'id': 'nvidia/nemotron-3-ultra-550b-a55b'}, {'max_tokens': 131072},
                  {'context_length': True}, {'context_length': 1.5}, {'context_length': '1M'}):
        assert extract_limits(value)[0] == {}
        assert discovery_limits(value)['context_metadata']['status'] == 'unknown'
        assert 'context_window' not in discovery_limits(value)


@pytest.mark.parametrize('entry,expected', [
    ({'context_length': 1048576}, {'context_window': 1048576}),
    ({'context_window': '1000000', 'max_output_tokens': 8192}, {'context_window': 1000000, 'max_output_tokens': 8192}),
    ({'context': 1048576, 'max-output': 8192}, {'context_window': 1048576, 'max_output_tokens': 8192}),
    ({'top_provider': {'context_length': 65536, 'max_completion_tokens': 4096}}, {'context_window': 65536, 'max_output_tokens': 4096}),
    ({'context_length': 1000000, 'top_provider': {'context_length': 65536}}, {'context_window': 1000000}),
])
def test_explicit_catalog_schemas(entry, expected):
    assert extract_limits(entry)[0] == expected


def test_live_fields_precede_published_capacity_and_keep_separate_provenance():
    endpoint = annotate_catalog({'context_length': 131072}, 'https://endpoint.test/models')
    published = annotate_catalog({'context': 1048576, 'max-output': 8192}, 'https://catalog.test/models')
    merged = merge_context_metadata(endpoint, published)
    assert merged['context_window'] == 131072
    assert merged['max_output_tokens'] == 8192
    assert merged['field_provenance']['max_output_tokens']['source'] == 'https://catalog.test/models'
    assert merged['field_provenance']['context_window']['source'] == 'https://endpoint.test/models'


def test_registry_refresh_persists_changed_catalog_limits_without_erasing_manual(tmp_path):
    db = tmp_path / 'state.db'
    registry = LocalModelRegistry('nvidia', ROOT / 'model-registry', db)
    entry = annotate_catalog({'id': 'nvidia/nemotron-3-ultra-550b-a55b', 'context': 1048576}, 'ngc')
    registry.register_catalog([entry])
    key = entry['id']
    assert registry.profiles[key].limits.context_window == 1048576
    registry.register_catalog([annotate_catalog({'id': key, 'context': 2097152}, 'ngc')])
    assert registry.profiles[key].limits.context_window == 2097152
    registry.register_catalog([{'id': key}])
    assert registry.profiles[key].limits.context_window == 2097152
    reloaded = LocalModelRegistry('nvidia', ROOT / 'model-registry', db)
    assert reloaded.profiles[key].limits.context_window == 2097152
    manual = ModelProfile('nvidia/vendor/manual', 'nvidia', 'vendor/manual', limits=LimitProfile(context_window=777))
    registry.register_profile(manual)
    registry.register_catalog([{'id': 'vendor/manual', 'context_length': 999}])
    assert registry.profiles[manual.canonical_id].limits.context_window == 777


def make_cache(tmp_path):
    store = ModelStateStore('nous', tmp_path / 'catalog.db')
    registry = LocalModelRegistry('nous', ROOT / 'model-registry', tmp_path / 'catalog.db')
    writes = []
    client = SimpleNamespace(schedule_catalog=lambda *args: writes.append(args))
    return CachedModelCatalog(store, registry, client, 'https://provider.test/v1/models', ttl_sec=60), writes


async def test_concurrent_cold_reads_fetch_once_and_restart_uses_cache(tmp_path):
    cache, writes = make_cache(tmp_path)
    calls = []
    async def fetch():
        calls.append(1)
        await asyncio.sleep(0.01)
        return [{'id': 'vendor/model', 'context_length': 1048576, 'future': {'opaque': True}}]
    models = await asyncio.gather(*(cache.get(fetch) for _ in range(25)))
    assert len(calls) == len(writes) == 1
    assert all(rows[0]['context_length'] == 1048576 for rows in models)
    models[0][0]['future']['opaque'] = False
    assert cache.snapshot()[0]['future']['opaque'] is True
    restarted, _ = make_cache(tmp_path)
    assert (await restarted.get(fetch))[0]['context_length'] == 1048576
    assert len(calls) == 1
    await cache.close()
    await restarted.close()


async def test_outage_keeps_original_age_and_throttles_cold_failure(tmp_path):
    cache, _ = make_cache(tmp_path)
    calls = []
    async def fail():
        calls.append(1)
        raise OSError('controlled outage')
    for _ in range(5):
        assert await cache.get(fail) == []
    assert len(calls) == 1
    async def fetch():
        return [{'id': 'vendor/model', 'context_window': 131072}]
    await cache.refresh(fetch, force=True)
    old = copy.deepcopy(cache.snapshot())
    await cache.refresh(fail, force=True)
    assert cache.snapshot() == old
    assert context_metadata(annotate_catalog({'context_length': 1000}, 'test', checked_at=1, ttl_sec=1))['stale']
    await cache.close()


def nvidia_registry(tmp_path, monkeypatch):
    assert 'nvidia-python' in ACTIVE
    spec = importlib.util.spec_from_file_location('isolated_context_registry', ROOT / 'nvidia-python/src/registry.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'CACHE_FILE', str(tmp_path / 'ngc.json'))
    return module


async def test_nvidia_ngc_exact_match_no_seed_unknown_output_and_atomic_cache(tmp_path, monkeypatch):
    module = nvidia_registry(tmp_path, monkeypatch)
    registry = module.Registry()
    assert registry.all() == {}
    async def fetch():
        entry = annotate_catalog({'id': 'nvidia/ultra', 'context': 1048576}, module.NGC_FEATURED_URL)
        return {'nvidia/ultra': entry}
    registry._fetch_live = fetch
    await registry.refresh(force=True)
    assert registry.get_official_context('nvidia/ultra')['context'] == 1048576
    assert registry.get_official_context('other/ultra') is None
    assert 'maxOutput' not in registry.get_official_context('nvidia/ultra')
    restored = module.Registry()
    before = restored.get_official_context('nvidia/ultra')
    async def fail():
        raise OSError('controlled outage')
    restored._fetch_live = fail
    await restored.refresh(force=True)
    assert restored.get_official_context('nvidia/ultra') == before
    assert not list(tmp_path.glob('tmp*'))


async def test_ngc_parser_real_schema_and_bad_entries_do_not_poison_catalog(tmp_path, monkeypatch):
    module = nvidia_registry(tmp_path, monkeypatch)
    data = {'featured-models': [None, {'model': 'bad', 'context': True},
                               {'model': 'nvidia/nemotron-3-ultra-550b-a55b', 'context': 1048576, 'max-output': 8192},
                               {'model': 'vendor/unknown-output', 'context': 200000}]}
    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return data
    calls = []
    def get(*args, **kwargs):
        calls.append(1)
        return Response()
    registry = module.Registry()
    registry.set_external_agent(SimpleNamespace(get=get))
    await registry.refresh(force=True)
    assert registry.get_official_context('nvidia/nemotron-3-ultra-550b-a55b')['context'] == 1048576
    assert len(registry.all()) == 2
    assert 'maxOutput' not in registry.get_official_context('vendor/unknown-output')
    for _ in range(5):
        await registry.refresh()
    assert calls == [1]


def test_scoped_error_limit_survives_restart_without_overwriting_catalog(tmp_path):
    path = tmp_path / 'state.db'
    store = ModelStateStore('nous', path)
    store.upsert_catalog([{'id': 'vendor/model', 'context_length': 1048576}])
    store.record_context_limit('vendor/model', 'synthetic-secret', 'https://route.test/v1/responses?secret=hidden',
                               {'context_window': 131072, 'input_tokens': 100000})
    fresh = ModelStateStore('nous', path)
    observation = fresh.context_observations('vendor/model')[0]
    assert observation['limits']['context_window'] == 131072
    assert observation['endpoint'] == 'https://route.test/v1/responses'
    assert 'synthetic-secret' not in json.dumps(observation)
    assert 'hidden' not in json.dumps(observation)
    assert fresh.get_catalog()[0]['context_length'] == 1048576


def _build_page(model, specifications):
    flight = '42:' + json.dumps(['$', '$L5a', None, {'spec': {
        'artifactName': model.split('/')[1], 'attributes': {'specifications': specifications}}}])
    return ('<meta property="og:url" content="https://build.nvidia.com/' + model + '/build">'
            '<script>self.__next_f.push(' + json.dumps([1, flight]) + ')</script>')


def test_build_page_requires_exact_model_and_numeric_structured_capacity():
    from common.nvidia_model_specs import parse_build_context, build_model_url
    model = 'nvidia/exact-model'
    page = _build_page(model, {'contextLength': 1048576})
    assert parse_build_context(page, model) == 1048576
    assert parse_build_context(page, 'other/exact-model') is None
    for value in ('1M', True, 0, -1, 1048576.5):
        assert parse_build_context(_build_page(model, {'contextLength': value}), model) is None
    assert parse_build_context('<p>context 1M, max_tokens=8192</p>', model) is None
    assert build_model_url('../elsewhere') is None
    conflict = page + _build_page(model, {'contextLength': 131072})
    assert parse_build_context(conflict, model) is None


async def test_build_metadata_background_cache_negative_cache_and_source_priority(tmp_path, monkeypatch):
    module = nvidia_registry(tmp_path, monkeypatch)
    monkeypatch.setenv('NVIDIA_BUILD_CONTEXT_ENABLED', 'true')
    registry = module.Registry()
    ids = ['vendor/documented', 'vendor/unknown', 'vendor/featured']
    registry.set_model_ids(lambda: ids)
    calls = []
    class Response:
        status = 200
        def __init__(self, model): self.model, self.content = model, self
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def iter_chunked(self, _):
            yield _build_page(self.model, {'contextLength': 1000000} if self.model != 'vendor/unknown' else {}).encode()
    def get(url, **kwargs):
        calls.append(url)
        return Response(url.removeprefix('https://build.nvidia.com/').removesuffix('/build'))
    registry.set_external_agent(SimpleNamespace(get=get))
    registry._map['vendor/featured'] = annotate_catalog({'id': 'vendor/featured', 'context': 131072}, module.NGC_FEATURED_URL)
    await registry.refresh_build_metadata()
    assert len(calls) == 2
    assert registry.get_official_context('vendor/documented')['context'] == 1000000
    assert registry.get_official_context('vendor/featured')['context'] == 131072
    assert registry.get_official_context('vendor/unknown') is None
    assert registry.get_official_context('vendor/documented')['context_metadata']['scope'] == 'nvidia_published_model_specification'
    await registry.refresh_build_metadata()
    assert len(calls) == 2
    restored = module.Registry()
    restored.set_external_agent(SimpleNamespace(get=get))
    restored.set_model_ids(lambda: ids)
    await restored.refresh_build_metadata()
    assert len(calls) == 2
    # A stale NGC record removed from the current featured list must not
    # permanently block a newer exact-model Build specification.
    restored._map['vendor/featured'] = annotate_catalog(
        {'id': 'vendor/featured', 'context': 131072}, module.NGC_FEATURED_URL,
        checked_at=1, ttl_sec=1)
    await restored.refresh_build_metadata()
    assert len(calls) == 3
    assert restored.get_official_context('vendor/featured')['context'] == 1000000


async def test_force_refresh_calls_coalesce(tmp_path):
    cache, _ = make_cache(tmp_path)
    calls = []
    async def fetch():
        calls.append(1)
        await asyncio.sleep(0.01)
        return [{'id': 'vendor/model', 'context_length': 1048576}]
    await asyncio.gather(*(cache.refresh(fetch, force=True) for _ in range(10)))
    assert len(calls) == 1


def test_nous_discovery_prefers_raw_catalog_and_removes_template_context():
    import ast
    path = ROOT / 'nous/src/main.py'
    tree = ast.parse(path.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'get_model_meta')
    template = {'context_window': 131072, 'max_context_window': 131072, 'max_tokens': 4096}
    namespace = {'resolve_model': lambda mid: 'vendor/real' if mid == 'sonnet' else mid,
                 '_load_model_info_template': lambda: template, 'MODEL_METADATA': {},
                 'is_alias_name': lambda mid: mid == 'sonnet', 'get_dynamic_alias_target': lambda: 'vendor/real',
                 'discovery_limits': discovery_limits}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    for model in ('vendor/real', 'sonnet'):
        raw = annotate_catalog({'id': 'vendor/real', 'context_length': 1048576, 'future_catalog': {'x': 1}}, 'nous')
        result = namespace['get_model_meta'](model, raw)
        assert result['context_window'] == result['max_context_window'] == 1048576
        assert result['future_catalog'] == {'x': 1}
        assert 'max_tokens' not in result
    unknown = namespace['get_model_meta']('vendor/unknown', {'id': 'vendor/unknown'})
    assert 'context_window' not in unknown and 'max_context_window' not in unknown
    assert template['context_window'] == 131072


async def test_nvidia_models_route_consumes_ngc_instead_of_losing_context():
    import ast
    from types import MethodType
    from test_nvidia_messages_request_controls import _module
    caps = _module('isolated_context_capabilities', ROOT / 'nvidia-python/src/capabilities.py')
    path = ROOT / 'nvidia-python/src/main.py'
    tree = ast.parse(path.read_text())
    names = {'_context_catalog_entry', '_enrich_model_metadata', 'enrich_model_metadata', 'models_route'}
    nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    for node in nodes: node.decorator_list = []
    async def statuses(): return {}
    model = 'nvidia/nemotron-3-ultra-550b-a55b'
    official = annotate_catalog({'id': model, 'context': 1048576, 'max-output': 8192}, 'ngc')
    server = SimpleNamespace(pool=SimpleNamespace(models_cached=[model], models_metadata={model: {'id': model}}),
        registry=SimpleNamespace(get_official_context=lambda mid: official if mid == model else None),
        metrics=SimpleNamespace(get_model_status=statuses), _model_status_view=lambda x: x)
    namespace = {'self': server, 'build_catalog': caps.build_catalog, 'BASE_LLM': 'https://integrate.api.nvidia.com',
                 'BASE_GENAI': 'http://fake', 'get_dynamic_alias_target': lambda: ''}
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[])), str(path), 'exec'), namespace)
    server._context_catalog_entry = MethodType(namespace['_context_catalog_entry'], server)
    server._enrich_model_metadata = MethodType(namespace['_enrich_model_metadata'], server)
    response = await namespace['models_route'](None)
    actual = next(m for m in response['data'] if m['id'] == model)
    assert actual['context_window'] == actual['context_length'] == actual['max_context_window'] == 1048576
    assert actual['max_output_tokens'] == 8192
    assert actual['context_metadata']['source'] == 'ngc'

    # Restart hydrates a merged record. A refreshed publication must replace
    # its prior value rather than being hidden behind the old normalized alias.
    server.pool.models_metadata[model] = actual
    official = annotate_catalog({'id': model, 'context': 2097152}, 'ngc')
    assert server._context_catalog_entry(model)['context_window'] == 2097152

    # A real endpoint observation has priority over a larger public capacity.
    server.pool.models_metadata[model] = annotate_catalog(
        {'id': model, 'context_length': 65536}, namespace['BASE_LLM'] + '/v1/models')
    assert server._context_catalog_entry(model)['context_window'] == 65536
    server.pool.models_metadata[model] = server._context_catalog_entry(model)
    assert server._context_catalog_entry(model)['context_window'] == 65536

    # Hosted NVIDIA publications cannot certify an independently configured NIM.
    namespace['BASE_LLM'] = 'http://custom-nim.test'
    custom = server._context_catalog_entry(model)
    assert 'context_window' not in custom
    server.pool.models_metadata[model] = annotate_catalog(
        {'id': model, 'context_length': 32768}, namespace['BASE_LLM'] + '/v1/models')
    assert server._context_catalog_entry(model)['context_window'] == 32768


@pytest.mark.parametrize('contents', ['[]', 'null', '{"schema": 2, "map": []}'])
def test_malformed_ngc_cache_is_ignored(tmp_path, monkeypatch, contents):
    module = nvidia_registry(tmp_path, monkeypatch)
    Path(module.CACHE_FILE).write_text(contents)
    assert module.Registry().all() == {}


async def test_custom_nvidia_endpoint_disables_published_fetch(tmp_path, monkeypatch):
    module = nvidia_registry(tmp_path, monkeypatch)
    registry = module.Registry(enabled=False)
    def forbidden(*args, **kwargs):
        pytest.fail('custom endpoint consulted public hosted metadata')
    registry.set_external_agent(SimpleNamespace(get=forbidden))
    registry.set_model_ids(lambda: ['nvidia/ultra'])
    assert await registry.refresh(force=True) == {}
    await registry.refresh_build_metadata()
    registry.start()
    assert registry._timer is None


async def test_cache_from_another_endpoint_or_without_provenance_is_refetched(tmp_path):
    cache, _ = make_cache(tmp_path)
    cache.store.upsert_catalog([
        {'id': 'legacy-guess', 'context_length': 131072},
        annotate_catalog({'id': 'different-endpoint', 'context_length': 1048576}, 'https://elsewhere/models')])
    assert cache.snapshot() == []
    calls = []
    async def fetch():
        calls.append(1)
        return [{'id': 'actual', 'context_length': 65536}]
    assert (await cache.get(fetch))[0]['context_length'] == 65536
    assert calls == [1]


async def test_ngc_callback_failure_does_not_abort_refresh(tmp_path, monkeypatch):
    module = nvidia_registry(tmp_path, monkeypatch)
    registry = module.Registry()
    async def fetch():
        return {'vendor/actual': annotate_catalog({'context': 1000}, module.NGC_FEATURED_URL)}
    def broken_callback():
        raise OSError('temporary downstream failure')
    registry._fetch_live, registry.on_refresh = fetch, broken_callback
    await registry.refresh(force=True)
    assert registry.get_official_context('vendor/actual')['context'] == 1000


async def test_nvidia_catalog_keeps_exact_deployment_ids(tmp_path, monkeypatch):
    from test_nvidia_messages_request_controls import POOL
    monkeypatch.setenv('NVIDIA_BASE_URL', 'http://isolated.test')
    models = [None, {'id': True}, {'id': 'vendor/model', 'context_length': 1000},
              {'id': 'stg/vendor/model', 'context_length': 2000},
              {'id': 'vendor/test/model', 'context_length': 3000}]
    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return {'data': models}
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, *args, **kwargs): return Response()
    monkeypatch.setattr(POOL.aiohttp, 'ClientSession', Session)
    pool = POOL.KeyPool()
    assert await pool.refresh_models(force=True) == ['stg/vendor/model', 'vendor/model', 'vendor/test/model']
    assert {key: value['context_metadata']['context_window'] for key, value in pool.models_metadata.items()} == {
        'vendor/model': 1000, 'stg/vendor/model': 2000, 'vendor/test/model': 3000}
