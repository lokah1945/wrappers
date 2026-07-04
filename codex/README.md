# wrapper-codex

Local FastAPI gateway for `codex exec`, designed to run beside `wrapper-nvidia`.

## Service

- Base URL: `http://127.0.0.1:9103`
- Upstream runtime: `codex exec --json`
- Main endpoint: `POST /v1/runs`
- Compatibility endpoint: `POST /v1/chat/completions`

Port `9101` was skipped because it is already occupied on this host.

## Example

```bash
curl -N http://127.0.0.1:9103/v1/runs \
  -H 'content-type: application/json' \
  -d '{"prompt":"Say ok","cwd":"/root","stream":true,"timeout_seconds":60}'
```

## Notes

This wrapper controls process concurrency, workspace policy, timeout, cancellation,
and metrics. It does not rotate provider API keys; Codex auth remains managed by
the local Codex installation.

## Systemd

A service unit template is included at `codex-wrapper.service`.
