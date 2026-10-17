"""Health identity evidence: git_commit must never claim more than it knows.

The 2026-10-17 incident: services were edited, restarted and verified live
BEFORE the fix was committed, so /health reported the pre-fix commit while
running post-fix code — the reported identity silently lagged reality.

Regression contract (all ACTIVE wrappers, frozen ones excluded):
  1. /health and /version must expose git_dirty alongside git_commit.
     git_commit + git_dirty (captured at import time) identify the
     actually-running code; commit alone is not evidence.
  2. _resolve_git_dirty() must report False on a clean tree, True on a dirty
     tree and None when git state cannot be determined.
"""

from __future__ import annotations

import ast
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

ACTIVE_WRAPPERS = (
    ('nvidia-python', 'src/main.py'),
    ('nous', 'src/main.py'),
    ('openrouter', 'src/main.py'),
)

RESOLVER_NAMES = ('_resolve_git_commit', '_resolve_git_dirty', '_resolve_git_root')


def _route_functions(tree, path_suffix):
    """All (async) function defs decorated with @app.get(<suffix>) anywhere in
    the tree (module level or nested inside class/method setup)."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == 'get' and dec.args
                    and isinstance(dec.args[0], ast.Constant)
                    and dec.args[0].value == path_suffix):
                found.append(node)
    return found


def _returned_dict_keys(func):
    """Keys of every dict literal returned directly by the function."""
    keys = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for key in node.value.keys:
                if isinstance(key, ast.Constant):
                    keys.add(key.value)
    return keys


@pytest.mark.parametrize('wrapper,path', ACTIVE_WRAPPERS)
def test_health_route_reports_git_identity(wrapper, path):
    tree = ast.parse((ROOT / wrapper / path).read_text())
    routes = _route_functions(tree, '/health')
    assert routes, f'{wrapper}: no /health route found'
    for route in routes:
        keys = _returned_dict_keys(route)
        assert 'git_commit' in keys, f'{wrapper}: /health lacks git_commit'
        assert 'git_dirty' in keys, f'{wrapper}: /health lacks git_dirty'


@pytest.mark.parametrize('wrapper,path', ACTIVE_WRAPPERS)
def test_version_route_reports_git_identity(wrapper, path):
    tree = ast.parse((ROOT / wrapper / path).read_text())
    routes = _route_functions(tree, '/version')
    assert routes, f'{wrapper}: no /version route found'
    for route in routes:
        keys = _returned_dict_keys(route)
        assert 'git_commit' in keys, f'{wrapper}: /version lacks git_commit'
        assert 'git_dirty' in keys, f'{wrapper}: /version lacks git_dirty'


@pytest.mark.parametrize('wrapper,path', ACTIVE_WRAPPERS)
def test_resolvers_are_defined(wrapper, path):
    src = (ROOT / wrapper / path).read_text()
    for name in RESOLVER_NAMES:
        assert f'def {name}(' in src, f'{wrapper}: missing {name}'
    assert 'GIT_DIRTY = _resolve_git_dirty()' in src, (
        f'{wrapper}: GIT_DIRTY not captured at import time')


def _extract_resolver_source(path: Path) -> str:
    tree = ast.parse(path.read_text())
    wanted = set(RESOLVER_NAMES)
    parts = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            parts.append(ast.get_source_segment(path.read_text(), node))
            wanted.discard(node.name)
    assert not wanted, f'missing resolvers in {path}: {sorted(wanted)}'
    return '\n\n'.join(parts)


def _load_resolvers(path: Path, fake_file: str):
    namespace = {'os': os, '__file__': fake_file}
    exec(_extract_resolver_source(path), namespace)  # noqa: S102 - test fixture
    return namespace


def _git(tmp_path: Path, *args):
    subprocess.run(['git', *args], cwd=tmp_path, check=True, capture_output=True)


@pytest.fixture()
def clean_repo(tmp_path):
    repo = tmp_path / 'wrapper'
    (repo / 'src').mkdir(parents=True)
    _git(repo, 'init')
    _git(repo, 'config', 'user.email', 'test@example.com')
    _git(repo, 'config', 'user.name', 'test')
    (repo / 'src' / 'main.py').write_text('print("v1")\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'v1')
    return repo


@pytest.mark.parametrize('wrapper,path', ACTIVE_WRAPPERS)
def test_resolver_reports_clean_tree(wrapper, path, clean_repo):
    fake_file = str(clean_repo / 'src' / 'main.py')
    resolvers = _load_resolvers(ROOT / wrapper / path, fake_file)
    head = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], cwd=clean_repo,
        capture_output=True, text=True, check=True).stdout.strip()
    assert resolvers['_resolve_git_commit']() == head
    assert resolvers['_resolve_git_dirty']() is False


@pytest.mark.parametrize('wrapper,path', ACTIVE_WRAPPERS)
def test_resolver_reports_dirty_tree(wrapper, path, clean_repo):
    (clean_repo / 'src' / 'main.py').write_text('print("v2-uncommitted")\n')
    fake_file = str(clean_repo / 'src' / 'main.py')
    resolvers = _load_resolvers(ROOT / wrapper / path, fake_file)
    assert resolvers['_resolve_git_dirty']() is True


@pytest.mark.parametrize('wrapper,path', ACTIVE_WRAPPERS)
def test_resolver_ignores_runtime_marker_changes(wrapper, path, clean_repo):
    """systemd ExecStartPre rewrites runtime/<name>.commit on every start;
    that bookkeeping must not flip git_dirty or the signal is useless."""
    runtime = clean_repo / 'runtime'
    runtime.mkdir()
    (runtime / 'nvidia-python.commit').write_text('deadbeef\n')
    _git(clean_repo, 'add', '-A')
    _git(clean_repo, 'commit', '-m', 'add markers')
    (runtime / 'nvidia-python.commit').write_text('cafebabe\n')
    fake_file = str(clean_repo / 'src' / 'main.py')
    resolvers = _load_resolvers(ROOT / wrapper / path, fake_file)
    assert resolvers['_resolve_git_dirty']() is False


@pytest.mark.parametrize('wrapper,path', ACTIVE_WRAPPERS)
def test_resolver_ignores_untracked_runtime_files(wrapper, path, clean_repo):
    runtime = clean_repo / 'runtime'
    runtime.mkdir()
    (runtime / 'untracked-state.json').write_text('{"x": 1}\n')
    fake_file = str(clean_repo / 'src' / 'main.py')
    resolvers = _load_resolvers(ROOT / wrapper / path, fake_file)
    assert resolvers['_resolve_git_dirty']() is False


@pytest.mark.parametrize('wrapper,path', ACTIVE_WRAPPERS)
def test_resolver_never_raises_outside_git(wrapper, path, tmp_path):
    outside = tmp_path / 'plain' / 'src'
    outside.mkdir(parents=True)
    fake_file = str(outside / 'main.py')
    resolvers = _load_resolvers(ROOT / wrapper / path, fake_file)
    # fallback root is /root/wrapper on the VPS — whatever tree it lands on,
    # the resolver must answer with a bool (or None) instead of raising
    result = resolvers['_resolve_git_dirty']()
    assert result is None or isinstance(result, bool)


def test_docstring_documents_why_dirty_flag_exists():
    for wrapper, path in ACTIVE_WRAPPERS:
        src = (ROOT / wrapper / path).read_text()
        segment = src.split('def _resolve_git_dirty', 1)[1].split('def ', 1)[0]
        assert 'git_commit alone is misleading' in textwrap.dedent(segment)
