"""Deployment must fail on unhealthy readiness and continue independent restarts."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

from productions import check_service_ready
from common.wrapper_scope import production_wrappers, select_services

ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return json.dumps(self.body).encode()


@pytest.mark.parametrize('status', [401, 429, 500, 503])
def test_readiness_probe_rejects_http_errors(monkeypatch, status):
    def fail(request, timeout):
        raise HTTPError(request.full_url, status, 'test', {}, io.BytesIO(b'private body'))
    monkeypatch.setattr(check_service_ready, 'urlopen', fail)
    ok, detail = check_service_ready.probe('http://127.0.0.1:1234', 'private-token')
    assert not ok
    assert str(status) in detail
    assert 'private' not in detail


def test_readiness_probe_checks_boolean_and_sends_token_in_header(monkeypatch):
    calls = []
    def reply(request, timeout):
        calls.append(request)
        return FakeResponse({'status': 'ok'} if request.full_url.endswith('/health')
                            else {'ready': False})
    monkeypatch.setattr(check_service_ready, 'urlopen', reply)
    assert check_service_ready.probe('http://127.0.0.1:1234', 'private-token') == (False, '/ready ready=false')
    assert len(calls) == 2
    assert all(request.get_header('Authorization') == 'Bearer private-token' for request in calls)


def test_registry_probe_uses_supported_health_surface(monkeypatch):
    calls = []
    def reply(request, timeout):
        calls.append(request.full_url)
        return FakeResponse({'status': 'ok'})
    monkeypatch.setattr(check_service_ready, 'urlopen', reply)
    assert check_service_ready.probe('http://127.0.0.1:9200', '', registry=True)[0]
    assert calls == ['http://127.0.0.1:9200/health']


@pytest.mark.parametrize('failure', ['restart', 'ready'])
def test_installer_reports_failure_after_attempting_all_services(tmp_path, failure):
    installer = tmp_path / 'install.sh'
    installer.write_text((ROOT / 'install.sh').read_text())
    (tmp_path / 'common').mkdir()
    (tmp_path / 'common/wrapper_scope.py').write_text((ROOT / 'common/wrapper_scope.py').read_text())
    (tmp_path / 'wrappers.json').write_text((ROOT / 'wrappers.json').read_text())
    names = sorted(select_services(), key=lambda name: name != "model-registry")
    for name in names:
        directory = tmp_path / name
        (directory / 'systemd').mkdir(parents=True)
        (directory / '.env').write_text('BEARER_TOKEN=test\n')
        (directory / 'systemd' / f'wrapper-{name}.service').write_text('[Service]\n')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    commands = {
        'install': '#!/bin/bash\nexit 0\n',
        'systemctl': '''#!/bin/bash
if [ "$2" = restart ]; then
  printf '%s\\n' "$3" >> "$WRAPPER_TEST_RESTART_LOG"
  if [ "$WRAPPER_TEST_FAILURE" = restart ] && [ "$3" = wrapper-nvidia-python.service ]; then
    exit 1
  fi
fi
exit 0
''',
        'python3': '''#!/bin/bash
if [[ "$1" == */common/wrapper_scope.py ]]; then
  exec "$WRAPPER_TEST_PYTHON" "$@"
fi
if [ "$WRAPPER_TEST_FAILURE" = ready ] && [[ "$*" == *9101* ]]; then
  echo '/ready ready=false'
  exit 1
fi
exit 0
''',
    }
    for name, content in commands.items():
        command = bin_dir / name
        command.write_text(content)
        command.chmod(0o755)
    log = tmp_path / 'restarts'
    env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}',
               WRAPPER_TEST_RESTART_LOG=str(log), WRAPPER_TEST_FAILURE=failure,
               WRAPPER_TEST_PYTHON=sys.executable)
    result = subprocess.run(['bash', str(installer), '--skip-deps'], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert log.read_text().splitlines() == [f'wrapper-{name}.service' for name in names]
    assert 'unhealthy after restart' in result.stderr
    assert all((tmp_path / name / '.env').stat().st_mode & 0o777 == 0o600 for name in names)
