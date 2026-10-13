#!/usr/bin/env python3
"""Check health and authenticated readiness without printing credentials/bodies."""

import argparse
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import dotenv_values


def probe(base_url: str, token: str, registry: bool = False) -> tuple[bool, str]:
    for route in ('health',) if registry else ('health', 'ready'):
        headers = {'Accept': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        try:
            with urlopen(Request(f'{base_url.rstrip("/")}/{route}', headers=headers), timeout=5) as response:
                if not 200 <= response.status < 300:
                    return False, f'/{route} HTTP {response.status}'
                body = json.loads(response.read(65536))
        except HTTPError as exc:
            return False, f'/{route} HTTP {exc.code}'
        except (URLError, TimeoutError, OSError):
            return False, f'/{route} unavailable'
        except (ValueError, UnicodeError):
            return False, f'/{route} invalid JSON'
        if not isinstance(body, dict):
            return False, f'/{route} invalid response'
        if route == 'ready' and body.get('ready') is not True:
            return False, '/ready ready=false'
        if route == 'health' and body.get('status') not in ('ok', 'healthy') and body.get('ok') is not True:
            return False, '/health unhealthy'
    return True, 'health and readiness passed' if not registry else 'registry health passed'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--registry', action='store_true')
    parser.add_argument('--wait-seconds', type=float, default=20)
    args = parser.parse_args()
    token = (dotenv_values(args.env_file).get('BEARER_TOKEN') or '').strip()
    deadline = time.monotonic() + max(0, args.wait_seconds)
    while True:
        ok, detail = probe(args.base_url, token, registry=args.registry)
        if ok or time.monotonic() >= deadline:
            print(detail)
            return 0 if ok else 1
        time.sleep(1)


if __name__ == '__main__':
    raise SystemExit(main())
