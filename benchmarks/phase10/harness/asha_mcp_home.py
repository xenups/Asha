#!/usr/bin/env python3
"""Minimal stdio MCP client for the benchmark host agent.

Each invocation starts a real ``python -m asha.mcp_server`` process, performs
``initialize``, issues exactly one request, prints the JSON result and exits.
No mocks, no in-process imports of the Asha package.

Usage:
  python asha_mcp.py list
  python asha_mcp.py call asha_status '{"root": "/path"}'
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ASHA_ROOT = os.environ.get("ASHA_HOME", "/home/amir/src/Asha")
PYTHON = os.environ.get("ASHA_PY", "/home/amir/src/Asha/.venv/bin/python")


def _session(state_dir: str, request: list[dict]) -> list[dict]:
    env = dict(os.environ)
    env["ASHA_STATE_DIR"] = state_dir
    proc = subprocess.Popen(
        [PYTHON, "-m", "asha.mcp_server"], cwd=ASHA_ROOT,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=env)
    replies = []
    try:
        frames = [{"jsonrpc": "2.0", "id": 0, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18",
                              "capabilities": {},
                              "clientInfo": {"name": "bench-host", "version": "1"}}}]
        for index, item in enumerate(request, start=1):
            frames.append({"jsonrpc": "2.0", "id": index, **item})
        for frame in frames:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(frame) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                raise SystemExit("server produced no response: "
                                 + (proc.stderr.read()[-1500:] if proc.stderr else ""))
            replies.append(json.loads(line))
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=30)
    return replies


def main(argv: list[str]) -> int:
    state = os.environ.get("ASHA_BENCH_STATE")
    if not state:
        print("ASHA_BENCH_STATE is not set", file=sys.stderr)
        return 2
    mode = argv[1] if len(argv) > 1 else "list"
    if mode == "list":
        reply = _session(state, [{"method": "tools/list", "params": {}}])[1]
        names = [tool["name"] for tool in reply["result"]["tools"]]
        print(json.dumps({"tools": names}))
        return 0
    if mode == "call" and len(argv) >= 4:
        arguments = json.loads(argv[3])
        reply = _session(state, [{"method": "tools/call",
                                  "params": {"name": argv[2],
                                             "arguments": arguments}}])[1]
        result = reply["result"]
        print(result["content"][0]["text"] if result.get("content") else json.dumps(result))
        return 0 if result.get("isError") is False else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
