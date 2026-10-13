# Unified wrapper dashboard

Run from the repository root with `python3 -m uvicorn dashboard.app:app --host 127.0.0.1 --port 9105`.
The global page reports lifecycle-active services using their ports in `wrappers.json`.
The dropdown sits above the dashboard content. Selecting an inference wrapper displays
its existing dashboard without changing that dashboard's layout. The registry selection
displays its service status because it has no existing dashboard page.

Pending providers appear as frozen metadata cards. The service never probes them or
opens their files. Provider credentials are never loaded; protected endpoints report
unknown/protected readiness. An operator may enter a wrapper token through the existing
dashboard token prompt. Browser storage is isolated per wrapper. Query string credentials
are removed, and only browser-supplied Authorization and x-api-key headers are forwarded.

The gateway permits GET on a fixed endpoint allowlist, targets only loopback metadata
ports, and rejects redirects, management operations and inference routes. Global status
publishes only selected numeric counters and metadata, not raw health responses or key lists.
The status cache lasts five seconds. Independent bounded probes allow offline services to
appear without preventing the other cards from rendering.

The supplied user-systemd unit listens on `0.0.0.0:9105`, consistent with the existing
host deployment. Choose `127.0.0.1` when access is provided through a reverse proxy.
