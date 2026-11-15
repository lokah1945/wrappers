"""Read-only diagnostic: fake keys, loopback upstream, no vendor inference."""
import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

import httpx
from aiohttp import web

ROOT = Path(os.environ.get('COARENA_AUDIT_REPO', Path(__file__).resolve().parents[2]))
OUT = Path(__file__).with_name('coarena_snapshot_checks.json')
sys.path.insert(0, str(ROOT))
os.environ['COARENA_API_KEYS'] = 'fake-first,fake-second'
os.environ['COARENA_API_KEY'] = ''
os.environ['BEARER_TOKEN'] = 'fake-local-token'
os.environ['REQUIRE_AUTH'] = 'true'
logging.disable(logging.CRITICAL)
spec = importlib.util.spec_from_file_location('coarena_snapshot', ROOT/'coarena/src/main.py')
provider = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider)


def error_types(exc):
    children = getattr(exc, 'exceptions', ())
    return [type(exc).__name__] + [name for child in children for name in error_types(child)]


async def main():
    state = {'case': '', 'calls': []}
    async def upstream(request):
        state['calls'].append({'method': request.method, 'path': request.path,
                               'query': request.query_string})
        state['last_raw'] = await request.read()
        if state['case'] == 'retry':
            if len(state['calls']) == 1:
                return web.json_response({'error': {'code':'rate_limit_exceeded'}}, status=429,
                                         headers={'Retry-After':'1'})
        if state['case'] == 'stream':
            response = web.StreamResponse(headers={'Content-Type':'text/event-stream'})
            await response.prepare(request)
            try:
                await response.write(b'data: {"model":"fake-model","text":"first"}\n\n')
                await asyncio.sleep(0.02)
                await response.write(b'data: [DONE]\n\n')
                await response.write_eof()
            except (ConnectionResetError, RuntimeError):
                pass
            return response
        if state['case'] == 'headers':
            return web.Response(body=b'{\n  "ok": true,\n  "unknown": 1\n}\n',
                                headers={'Content-Type':'application/json',
                                         'Connection':'keep-alive',
                                         'Set-Cookie':'fake-cookie=example'})
        return web.json_response({'ok':True, 'model':'fake-model'})

    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', upstream)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    provider.COARENA_BASE_URL = f'http://127.0.0.1:{port}/api/v1'
    provider.COARENA_ANTHROPIC_BASE_URL = provider.COARENA_BASE_URL
    results = []
    body = {'model':'fake-model', 'messages':[{'role':'user','content':'hello'}]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=provider.app),
                                 base_url='http://test-wrapper') as client:
        native_results = []
        for path in ['/v1/chat/completions','/v1/responses','/v1/messages']:
            for model in ['claude-test-only','gpt-test-only']:
                state.update(case='native',calls=[],last_raw=b'')
                native_body = {**body,'model':model,'future_field':{'opaque':[None,1]}}
                wire = json.dumps(native_body,ensure_ascii=False,indent=1).encode()
                response = await client.post(path,content=wire,headers={'Content-Type':'application/json'})
                native_results.append({'client_path':path,'model':model,
                    'upstream_path':state['calls'][0]['path'],
                    'body_bytes_preserved':state['last_raw']==wire,
                    'status':response.status_code})
        results.append({'id':'P01','check':'native_paths_ignore_model_brand',
                        'note':'Fake upstream does not validate the payload dialect',
                        'cases':native_results,
                        'passed':all(r['body_bytes_preserved'] and r['upstream_path']=='/api'+r['client_path'] for r in native_results)})

        state.update(case='auth', calls=[])
        response = await client.post('/v1/chat/completions', json=body)
        results.append({'id':'D01', 'check':'local_auth_missing',
                        'expected':'401 without invoking upstream',
                        'observed_status':response.status_code, 'upstream_calls':len(state['calls']),
                        'finding_reproduced':response.status_code == 200 and len(state['calls']) == 1})

        state.update(case='retry', calls=[])
        response = await client.post('/v1/chat/completions', json=body)
        results.append({'id':'D02','check':'retry_available_second_key',
                        'expected':'second fake key succeeds in same request',
                        'observed_status':response.status_code,'upstream_calls':len(state['calls']),
                        'finding_reproduced':response.status_code == 429 and len(state['calls']) == 1})

        state.update(case='stream', calls=[])
        try:
            response = await client.post('/v1/chat/completions', json={**body,'stream':True})
            results.append({'id':'D03','check':'stream_resource_lifetime',
                            'observed_status':response.status_code,
                            'terminal_present':b'[DONE]' in response.content,
                            'finding_reproduced':b'[DONE]' not in response.content})
        except Exception as exc:
            results.append({'id':'D03','check':'stream_resource_lifetime',
                            'error_types':error_types(exc),'finding_reproduced':True})

        state.update(case='headers', calls=[])
        response = await client.post('/v1/chat/completions', json=body)
        results.append({'id':'D04','check':'response_header_framing',
                        'upstream_content_length_reused':int(response.headers['content-length']),
                        'actual_body_length':len(response.content),
                        'connection_header_forwarded':'connection' in response.headers,
                        'cookie_forwarded':'set-cookie' in response.headers,
                        'finding_reproduced':int(response.headers['content-length']) != len(response.content)})

        state.update(case='continuation', calls=[])
        response = await client.get('/v1/responses/resp_fake?include=output')
        results.append({'id':'D05','check':'catchall_version_prefix_query',
                        'expected_path':'/api/v1/responses/resp_fake','expected_query':'include=output',
                        'observed':state['calls'],
                        'finding_reproduced':state['calls'][0]['path'].startswith('/api/v1/v1/')
                            and state['calls'][0]['query']==''})

        state.update(case='management', calls=[])
        response = await client.delete('/key')
        results.append({'id':'D06','check':'inference_catchall_can_reach_management',
                        'expected':'route rejected locally without upstream credential use',
                        'observed_status':response.status_code,'observed':state['calls'],
                        'finding_reproduced':state['calls'][0]['path']=='/api/v1/key'})

        state.update(case='ready', calls=[])
        response = await client.get('/ready')
        results.append({'id':'D07','check':'readiness_not_local',
                        'observed':state['calls'],'local_ready_field':response.json().get('ready'),
                        'finding_reproduced':state['calls'][0]['path']=='/api/v1/ready'})

        old_iter = provider.iter_chunks_with_idle
        from common.sse import IDLE
        async def immediate_idle(resp, idle):
            yield IDLE
        provider.iter_chunks_with_idle = immediate_idle
        state.update(case='idle', calls=[])
        try:
            response = await client.post('/v1/chat/completions',json={**body,'stream':True})
            results.append({'id':'D08','check':'idle_sentinel_serialization',
                            'finding_reproduced':False})
        except Exception as exc:
            results.append({'id':'D08','check':'idle_sentinel_serialization',
                            'note':'IDLE yielded immediately by controlled helper stub',
                            'error_types':error_types(exc),'finding_reproduced':True})
        finally:
            provider.iter_chunks_with_idle = old_iter

    await runner.cleanup()
    unit = ROOT/'coarena/systemd/wrapper-coarena.service'
    results.append({'id':'D09','check':'installer_and_entrypoint',
                    'expected_unit_present':unit.exists(),
                    'main_at_configured_workdir_present':(ROOT/'coarena/main.py').exists(),
                    'src_main_present':(ROOT/'coarena/src/main.py').exists(),
                    'finding_reproduced':not unit.exists() and not (ROOT/'coarena/main.py').exists()})
    payload={'source_head':'5a02a0cb1773327fb719a8ab4c0412a187d5e60a',
             'timestamp_utc':datetime.now(timezone.utc).isoformat(),
             'scope':'snapshot diagnostic, fake keys, loopback upstream, ASGI transport; no real inference or deployment',
             'results':results}
    OUT.write_text(json.dumps(payload,indent=2)+'\n')
    print(json.dumps(payload,indent=2))


asyncio.run(main())
