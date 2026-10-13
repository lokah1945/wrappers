"""A selected release resolves dependencies once, before deployment mutation."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from common.wrapper_scope import ROOT, select_wrappers, select_services


def _installer_fixture(tmp_path):
    repo = tmp_path / 'repository'
    repo.mkdir()
    (repo / 'wrappers.json').write_text((ROOT / 'wrappers.json').read_text())
    (repo / 'common').mkdir()
    (repo / 'common/wrapper_scope.py').write_text((ROOT / 'common/wrapper_scope.py').read_text())
    (repo / 'tests').mkdir()
    (repo / 'tests/constraints-verified.txt').write_text('fastapi==0.142.2\n')
    (repo / 'install.sh').write_text((ROOT / 'install.sh').read_text())
    # Construct only lifecycle-active fixture paths; no frozen implementation
    # or real operator configuration is accessed.
    for name in select_services(repo=repo):
        directory = repo / name
        (directory / 'systemd').mkdir(parents=True)
        (directory / 'requirements.txt').write_text('fastapi>=0.115\n')
        (directory / '.env.example').write_text('BEARER_TOKEN=synthetic\n')
        (directory / f'systemd/wrapper-{name}.service').write_text(
            '[Service]\nWorkingDirectory=/root/wrapper/' + name + '\n')

    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    pip_log = tmp_path / 'pip.jsonl'
    mutation_log = tmp_path / 'mutations.jsonl'
    shim = bin_dir / 'python3'
    shim.write_text(
        '#!' + sys.executable + '\n'
        'import json,os,sys\n'
        'if sys.argv[1:3] == ["-m","pip"]:\n'
        '    with open(os.environ["INSTALL_TEST_PIP_LOG"],"a") as output:\n'
        '        output.write(json.dumps(sys.argv[1:])+"\\n")\n'
        '    failure=os.environ.get("INSTALL_TEST_FAIL_PHASE","")\n'
        '    sys.exit(23 if sys.argv[3] == failure else 0)\n'
        'os.execv(sys.executable,[sys.executable,*sys.argv[1:]])\n')
    shim.chmod(0o755)
    # Keep HOME unchanged. Unit operations are intercepted entirely, so these
    # tests cannot create directories, install units or call the user manager.
    for command in ('mkdir', 'install', 'systemctl'):
        shim = bin_dir / command
        shim.write_text(
            '#!' + sys.executable + '\n'
            'import json,os,sys\n'
            'with open(os.environ["INSTALL_TEST_MUTATION_LOG"],"a") as output:\n'
            '    output.write(json.dumps(sys.argv)+"\\n")\n')
        shim.chmod(0o755)
    env = {**os.environ, 'PYTHON_BIN': sys.executable,
           'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
           'INSTALL_TEST_PIP_LOG': str(pip_log),
           'INSTALL_TEST_MUTATION_LOG': str(mutation_log)}
    return repo, env, pip_log, mutation_log


def _run(repo, env, *args):
    return subprocess.run(['bash', str(repo / 'install.sh'), '--no-restart', *args],
                          cwd=repo, env=env, capture_output=True, text=True, timeout=15)


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize('requested', [None, 'nous'])
def test_dependencies_resolve_selected_services_once_before_unit_operations(tmp_path, requested):
    repo, env, pip_log, mutation_log = _installer_fixture(tmp_path)
    args = ('--wrapper', requested) if requested else ()
    result = _run(repo, env, *args)
    assert result.returncode == 0, result.stderr
    calls = _lines(pip_log)
    assert len(calls) == 2
    install, check = calls
    assert install[:3] == ['-m', 'pip', 'install']
    assert install[3:5] == ['-c', str(repo / 'tests/constraints-verified.txt')]
    selected = select_services([requested] if requested else None, repo=repo)
    requirement_paths = [install[index + 1] for index, value in enumerate(install) if value == '-r']
    assert set(requirement_paths) == {str(repo / name / 'requirements.txt') for name in selected}
    assert len(requirement_paths) == len(selected)
    assert check == ['-m', 'pip', 'check']
    assert _lines(mutation_log)


@pytest.mark.parametrize('failure_phase', ['install', 'check'])
def test_dependency_failure_precedes_environment_or_unit_changes(tmp_path, failure_phase):
    repo, env, pip_log, mutation_log = _installer_fixture(tmp_path)
    env['INSTALL_TEST_FAIL_PHASE'] = failure_phase
    result = _run(repo, env, '--wrapper', 'nous')
    assert result.returncode == 23
    assert len(_lines(pip_log)) == (1 if failure_phase == 'install' else 2)
    assert _lines(mutation_log) == []
    assert all(not (repo / name / '.env').exists() for name in select_wrappers(repo=repo))


def test_skip_dependencies_never_invokes_pip(tmp_path):
    repo, env, pip_log, mutation_log = _installer_fixture(tmp_path)
    result = _run(repo, env, '--wrapper', 'nous', '--skip-deps')
    assert result.returncode == 0, result.stderr
    assert _lines(pip_log) == []
    assert _lines(mutation_log)
