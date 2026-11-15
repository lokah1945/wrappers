"""Isolated snapshot diagnostics; no vendor calls, real keys or provider startup."""
from __future__ import annotations
import ast
import asyncio
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import *
from unittest.mock import patch

ROOT=Path(os.environ.get('WRAPPERS_AUDIT_REPO',Path(__file__).resolve().parents[2]))
OUT=Path(os.environ.get('WRAPPERS_AUDIT_OUTPUT',Path(__file__).with_name('fleet_snapshot_diagnostics.json')))
sys.path.insert(0,str(ROOT))
from common.wrapper_scope import select_wrappers
import aiohttp
from dotenv import load_dotenv as python_load_dotenv
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileMovedEvent

logging.disable(logging.CRITICAL)
HEAD=os.environ.get('WRAPPERS_AUDIT_HEAD','UNVERIFIED_SOURCE_HEAD')
ACTIVE=select_wrappers(repo=ROOT,include_registry=False)
rows=[]

def extract(path,names,namespace=None):
    tree=ast.parse(path.read_text())
    found=[n for n in ast.walk(tree) if isinstance(n,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
    assert len(found)==len(names),(path,names)
    for node in found:node.decorator_list=[]
    ns=dict(globals())
    ns.update(namespace or {})
    code=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*found],type_ignores=[])
    exec(compile(ast.fix_missing_locations(code),str(path),'exec'),ns)
    return ns

def pool_for(provider):
    path=ROOT/provider/'src'/('main.py' if provider=='nous' else 'key_pool.py')
    ns=extract(path,{'KeyEntry','KeyPool'},{'logger':logging.getLogger('fleet-diagnostic')})
    return ns['KeyPool']()

async def diagnostics():
    for provider in ACTIVE:
        prefix='NVIDIA' if provider=='nvidia-python' else provider.upper()
        first='fake-primary-credential-00001'
        second='fake-secondary-credential-00002'
        with patch.dict(os.environ,{prefix+'_API_KEY_1':first},clear=True):
            pool=pool_for(provider)
            pool.load_from_env()
            old=pool.keys[0]
            old.in_flight=2
            old.hard_blocked_until=time.time()+100
            old.total_requests=7
            os.environ[prefix+'_API_KEY_2']=second
            if provider=='nvidia-python':await pool.sync_keys([first,second])
            else:pool.load_from_env()
            retained=next(k for k in pool.keys if k.api_key==first)
            rows.append({'id':'H1','provider':provider,'check':'explicit_pool_refresh_retains_state',
                'note':'Calls pool refresh directly; this does not prove a runtime file watcher exists',
                'new_key_loaded':len(pool.keys)==2,'same_entry_object':retained is old,
                'in_flight':retained.in_flight,'cooldown_retained':retained.hard_blocked_until>time.time(),
                'request_counter':retained.total_requests})
            if provider=='nvidia-python':
                changed=await pool.sync_keys([])
                rows.append({'id':'H2','provider':provider,'check':'remove_last_key_via_sync',
                             'changed':changed,'remaining_keys':len(pool.keys)})

            with tempfile.TemporaryDirectory(prefix='fleet-env-') as directory:
                target=Path(directory)/'.env'
                target.write_text(prefix+'_API_KEY_1='+first+'\n'+prefix+'_API_KEY_2='+second+'\n')
                if provider=='nous':
                    loader=extract(ROOT/provider/'src/main.py',{'load_dotenv'})['load_dotenv']
                    previous=Path.cwd()
                    os.chdir(directory)
                    def reload_file():
                        with patch('os.path.expanduser',return_value=str(Path(directory)/'missing-home-env')):
                            loader(override=True)
                else:
                    def reload_file():python_load_dotenv(target,override=True)
                try:
                    reload_file()
                    target.write_text(prefix+'_API_KEY_2='+second+'\n')
                    reload_file()
                    pool.load_from_env()
                    rows.append({'id':'H3','provider':provider,'check':'deleted_env_assignment_after_overwrite_reload',
                        'deleted_key_remains_in_process_env':prefix+'_API_KEY_1' in os.environ,
                        'deleted_key_remains_selectable_in_pool':any(k.api_key==first for k in pool.keys),
                        'note':'Exercises the actual dotenv parser and actual pool; explicit refresh for providers without watcher'})
                finally:
                    if provider=='nous':os.chdir(previous)

    for provider in ('nvidia-python','nous'):
        if provider not in ACTIVE:continue
        captures=[]
        class Observer:
            def schedule(self,handler,**kwargs):captures.append(handler)
            def start(self):pass
        calls=[]
        with tempfile.TemporaryDirectory(prefix='fleet-watch-') as directory:
            filepath=Path(directory)/'src/main.py'
            filepath.parent.mkdir()
            filepath.write_text('')
            ns=extract(ROOT/provider/'src/main.py',{'start_env_watcher'},dict(
                __file__=str(filepath),HAS_WATCHDOG=True,Observer=Observer,
                logger=logging.getLogger('fleet-diagnostic'),
                load_dotenv=lambda **kwargs:calls.append('parse'),
                _ENV_RELOAD_CALLBACKS=[lambda:calls.append('sync')],
                KEY_POOL=SimpleNamespace(load_from_env=lambda:calls.append('sync'))))
            with patch.dict(os.environ,{},clear=True):
                ns['start_env_watcher']()
                handler=captures[0]
                handler.dispatch(FileModifiedEvent(str(Path(directory)/'.env')))
                modified=list(calls)
                calls.clear()
                handler.dispatch(FileMovedEvent(str(Path(directory)/'.env.tmp'),str(Path(directory)/'.env')))
                rows.append({'id':'H4','provider':provider,'check':'atomic_replace_event_dispatch',
                    'modified_event_calls':modified,'moved_event_calls':list(calls),
                    'note':'Actual handler dispatched with watchdog events; observer registration is stubbed'})

    with patch.dict(os.environ,{'NVIDIA_API_KEY_1':'fake-new-credential-00001'},clear=True):
        calls=[]
        class Pool:
            async def sync_keys(self,keys):calls.append('synced')
        ns=extract(ROOT/'nvidia-python/src/main.py',{'_sync_pool_from_env'},dict(
            self=SimpleNamespace(pool=Pool()),logger=logging.getLogger('fleet-diagnostic')))
        await asyncio.to_thread(ns['_sync_pool_from_env'])
        await asyncio.sleep(0)
        rows.append({'id':'H5','provider':'nvidia-python','check':'watcher_callback_in_background_thread',
                     'sync_executed':bool(calls),'note':'Actual callback in thread without an asyncio loop, matching a normal observer callback thread'})

    for provider in ('openrouter','coarena'):
        if provider not in ACTIVE:continue
        tree=ast.parse((ROOT/provider/'src/main.py').read_text())
        calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and (
            isinstance(n.func,ast.Name) and n.func.id in ('Observer','start_env_watcher') or
            isinstance(n.func,ast.Attribute) and n.func.attr in ('schedule','load_from_env'))]
        rows.append({'id':'H6','provider':provider,'check':'source_runtime_refresh_calls',
            'calls':[{'line':n.lineno,'call':ast.unparse(n.func)} for n in calls],
            'note':'Source inspection; no runtime hot-swap claim follows from having a load_from_env method'})


def duplicate_inventory():
    class Normalize(ast.NodeTransformer):
        def visit_Constant(self,node):
            if isinstance(node.value,str):
                node.value=re.sub('coarena|openrouter','PROVIDER',node.value,flags=re.I)
            return node
        def visit_FunctionDef(self,node):
            node=self.generic_visit(node)
            if node.body and isinstance(node.body[0],ast.Expr) and isinstance(node.body[0].value,ast.Constant) and isinstance(node.body[0].value.value,str):node.body=node.body[1:]
            return node
    groups={}
    sizes=[]
    for provider in ACTIVE:
        for filename in ('main.py','key_pool.py','metrics.py'):
            path=ROOT/provider/'src'/filename
            if not path.exists():continue
            text=path.read_text()
            tree=ast.parse(text)
            sizes.append({'provider':provider,'file':filename,'lines':len(text.splitlines()),
                          'top_level_basewrapper_subclasses':[n.name for n in tree.body if isinstance(n,ast.ClassDef) and any('BaseWrapper' in ast.unparse(b) for b in n.bases)]})
            if provider not in ('coarena','openrouter'):continue
            for node in ast.walk(tree):
                if not isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) or node.end_lineno-node.lineno+1<10:continue
                digest=hashlib.sha256(ast.dump(Normalize().visit(copy.deepcopy(node)),include_attributes=False).encode()).hexdigest()
                groups.setdefault(digest,[]).append({'provider':provider,'file':filename,'function':node.name,'line':node.lineno,'lines':node.end_lineno-node.lineno+1})
    duplicated=[g for g in groups.values() if len({x['provider'] for x in g})>1]
    return {'method':'Function AST equality after replacing only Coarena/OpenRouter strings; minimum 10 source lines; evidence of candidates, not proof of interchangeable semantics or RAM savings',
            'files':sizes,'matched_groups':len(duplicated),'matched_functions':duplicated}


asyncio.run(diagnostics())
result={'source_head':HEAD,'timestamp_utc':datetime.now(timezone.utc).isoformat(),
        'scope':'snapshot AST-executed pool/parser/handler diagnostics with fake credentials; no vendor inference or production runtime',
        'active_inference_providers':list(ACTIVE),'diagnostics':rows,'duplication_inventory':duplicate_inventory()}
OUT.parent.mkdir(parents=True,exist_ok=True)
OUT.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({'diagnostics':rows,'matched_AST_groups':result['duplication_inventory']['matched_groups']},indent=2))
