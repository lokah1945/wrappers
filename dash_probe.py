#!/usr/bin/env python3
"""Dashboard wiring audit — empirically probe every endpoint each dashboard consumes.

Tests both direct loopback (wrapper port) and via the unified gateway (9105 /wrappers/{name}/...).
Reports HTTP status, content-type, JSON shape, model counts — so mismatches with the
dashboard JS expectations become visible.
"""
import json
import sys
import urllib.request
import urllib.error

WRAPPERS = {
    "nvidia-python": 9101,
    "nous": 9102,
    "openrouter": 9103,
    "model-registry": 9200,
}
GATEWAY = 9106

# Endpoints consumed by dashboards (template + openrouter variant + gateway status page)
ENDPOINTS = [
    "/dashboard",
    "/health",
    "/ready",
    "/stats",
    "/metrics?window=60",
    "/metrics/models?window=60",
    "/metrics/model-status",
    "/v1/models",
    "/metrics/keys?window=60",
    "/metrics/activity?limit=50",
    "/metrics/chart/hourly?hours=24",
    "/events",
    "/catalog/health",
]


def fetch(url, timeout=5, stream_peek=False):
    if stream_peek:
        return fetch_stream_peek(url, deadline=2.5)
    req = urllib.request.Request(url, headers={"Accept": "application/json, text/event-stream, text/html"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            ctype = r.headers.get("Content-Type", "")
            body = r.read(4 * 1024 * 1024)
            return status, ctype, body, False
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()[:2048], False
    except Exception as e:
        return None, "", f"{type(e).__name__}: {e}".encode(), False


def fetch_stream_peek(url, deadline=2.5):
    """Raw-socket SSE peek: read status line + headers + first bytes, then hard-close."""
    import socket
    from urllib.parse import urlparse
    p = urlparse(url)
    try:
        sock = socket.create_connection((p.hostname, p.port), timeout=deadline)
    except Exception as e:
        return None, "", f"{type(e).__name__}: {e}".encode(), True
    sock.settimeout(deadline)
    req = (f"GET {p.path}{'?' + p.query if p.query else ''} HTTP/1.1\r\n"
           f"Host: {p.hostname}:{p.port}\r\nAccept: text/event-stream\r\n"
           f"Connection: close\r\n\r\n")
    sock.sendall(req.encode())
    buf = b""
    import time as _t
    end = _t.time() + deadline
    while _t.time() < end and len(buf) < 2048:
        try:
            chunk = sock.recv(1024)
            if not chunk:
                break
            buf += chunk
        except socket.timeout:
            break
        except Exception:
            break
    sock.close()
    if not buf:
        return None, "", b"no response within deadline", True
    head, _, rest = buf.partition(b"\r\n\r\n")
    try:
        status = int(head.split(b" ", 2)[1])
    except Exception:
        status = None
    ctype = ""
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-type:"):
            ctype = line.split(b":", 1)[1].decode().strip()
    return status, ctype, rest, True


def summarize(body: bytes):
    """Extract key shape info from JSON body."""
    try:
        data = json.loads(body)
    except Exception:
        text = body[:200].decode("utf-8", "replace")
        return {"shape": "non-JSON", "head": text.replace("\n", " ")[:160]}
    if isinstance(data, dict):
        out = {"keys": sorted(data.keys())[:24]}
        # interesting counts
        for k in ("models", "data", "states", "rows", "keys"):
            v = data.get(k)
            if isinstance(v, list):
                out[f"n_{k}"] = len(v)
                if v and isinstance(v[0], dict):
                    out[f"{k}[0].keys"] = sorted(v[0].keys())[:14]
                elif v and k == "data":
                    out["data[0]"] = (v[0] if isinstance(v[0], str) else v[0])[:80] if isinstance(v[0], str) else str(v[0])[:80]
            elif isinstance(v, dict):
                out[f"n_{k}"] = len(v)
        if "total_requests" in data:
            out["total_requests"] = data["total_requests"]
        return out
    if isinstance(data, list):
        return {"shape": "list", "n": len(data)}
    return {"shape": type(data).__name__}


def main():
    via_gateway = "--gateway" in sys.argv
    only = None
    for a in sys.argv[1:]:
        if not a.startswith("-"):
            only = a
    report = {}
    for name, port in WRAPPERS.items():
        if only and only != name:
            continue
        report[name] = {}
        for ep in ENDPOINTS:
            if name == "model-registry" and ep not in ("/dashboard", "/health", "/ready", "/stats", "/events", "/version", "/metrics", "/v1/models", "/catalog/health"):
                continue  # registry is only probed for status in the gateway, but check a few
            if via_gateway:
                path = ep.split("?")[0]
                url = f"http://127.0.0.1:{GATEWAY}/wrappers/{name}{path}" + (f"?{ep.split('?')[1]}" if "?" in ep else "")
            else:
                url = f"http://127.0.0.1:{port}{ep}"
            status, ctype, body, streamed = fetch(url, stream_peek=(ep == "/events"))
            entry = {"status": status, "ctype": ctype.split(";")[0]}
            if status == 200 and "json" in ctype:
                entry.update(summarize(body))
            elif status == 200 and "html" in ctype:
                entry["bytes"] = len(body)
                entry["has_bridge"] = b"unified-dashboard-bridge" in body
            elif streamed:
                entry["head"] = body[:120].decode("utf-8", "replace").replace("\n", "\\n")
            elif status is not None:
                entry["body_head"] = body[:120].decode("utf-8", "replace").replace("\n", " ")
            report[name][ep] = entry
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
