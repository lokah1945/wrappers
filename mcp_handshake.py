#!/usr/bin/env python3
"""Canonical single-connection MCP handshake test.

Runs ON the VPS: python3 mcp_handshake.py <port> <token>
Mirrors a real MCP client: ONE SSE stream kept open for the whole session,
JSON-RPC POSTs to the endpoint event's URL, replies read from the stream.
"""
import asyncio
import json
import sys

import aiohttp


def parse_blocks(buffer: str):
    """Yield (event, data) pairs from an SSE buffer, CRLF-tolerant."""
    out = []
    while "\n\n" in buffer or "\r\n\r\n" in buffer:
        if ("\r\n\r\n" in buffer and buffer.find("\r\n\r\n") <
                (buffer.find("\n\n") if "\n\n" in buffer else len(buffer))):
            block, buffer = buffer.split("\r\n\r\n", 1)
        else:
            block, buffer = buffer.split("\n\n", 1)
        ev, data = None, None
        for line in block.replace("\r\n", "\n").splitlines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        out.append((ev, data))
    return out, buffer


async def main(port: int, token: str) -> int:
    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": f"Bearer {token}"}
    results = []

    async with aiohttp.ClientSession() as session:
        # One SSE stream for the whole session
        async with session.get(f"{base}/mcp/sse", headers=headers,
                               timeout=aiohttp.ClientTimeout(total=45)) as sse:
            if sse.status != 200:
                print(f"FAIL sse-open HTTP {sse.status}")
                return 1
            results.append(("sse-open", True, f"HTTP {sse.status}"))

            buffer = ""
            endpoint_url = None
            # Read events from the stream while POSTing in lockstep
            async def read_until(pred, timeout_s=20):
                nonlocal buffer, endpoint_url
                try:
                    async def _read():
                        nonlocal buffer, endpoint_url
                        async for raw in sse.content:
                            buffer += raw.decode("utf-8", errors="replace")
                            blocks, buffer = parse_blocks(buffer)
                            for ev, data in blocks:
                                if ev == "endpoint" and data and not endpoint_url:
                                    endpoint_url = (data if data.startswith("http")
                                                    else f"{base}{data}")
                                if pred(ev, data):
                                    return (ev, data)
                        return None
                    return await asyncio.wait_for(_read(), timeout=timeout_s)
                except asyncio.TimeoutError:
                    return None

            # 1. endpoint event
            ev = await read_until(lambda e, d: e == "endpoint")
            if not endpoint_url:
                print(f"FAIL no endpoint event, buffer={buffer[:200]!r}")
                for n, s, d in results:
                    print(("PASS" if s else "FAIL") + f" | {n} | {d}")
                return 1
            results.append(("endpoint-event", True, endpoint_url))

            async def post(payload):
                async with session.post(endpoint_url, headers={**headers, "Content-Type": "application/json"}, json=payload) as r:
                    body = await r.text()
                    return r.status, body[:150]

            # 2. initialize
            init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "mcp-handshake-test", "version": "0.1"}}}
            # Send the POST AFTER the endpoint is known (stream still open).
            post_task = asyncio.create_task(post(init))
            reply = await read_until(lambda e, d: e == "message" and d and '"id":1' in d.replace(" ", ""))
            status, body = await post_task
            results.append(("initialize-post", status == 202 or (status == 200 and "result" in body), f"HTTP {status}"))
            if not reply:
                results.append(("initialize-reply", False, "no reply on stream"))
                for n, s, d in results:
                    print(("PASS" if s else "FAIL") + f" | {n} | {d}")
                return 1
            server_info = ""
            try:
                server_info = json.loads(reply[1])["result"].get("serverInfo", {})
                server_info = f"{server_info.get('name', '?')} v{server_info.get('version', '?')}"
            except Exception:
                pass
            results.append(("initialize-reply", True, server_info))

            # 3. notifications/initialized + tools/list
            note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            status, body = await post(note)
            results.append(("initialized-notify", status in (200, 202), f"HTTP {status}"))
            tl = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            post_task = asyncio.create_task(post(tl))
            reply = await read_until(lambda e, d: e == "message" and d and '"id":2' in d.replace(" ", ""))
            status, body = await post_task
            results.append(("tools/list-post", status in (200, 202), f"HTTP {status}"))
            if not reply:
                results.append(("tools/list", False, "no reply on stream"))
            else:
                tools = json.loads(reply[1])["result"].get("tools", [])
                names = ", ".join(t.get("name", "?") for t in tools)
                results.append(("tools/list", True, f"{len(tools)} tools: {names}"))

    failures = [r for r in results if not r[1]]
    for n, s, d in results:
        print(("PASS" if s else "FAIL") + f" | {n} | {d}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]), sys.argv[2])))
