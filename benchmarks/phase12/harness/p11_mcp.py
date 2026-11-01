#!/usr/bin/env python3
"""Phase 11 stdio MCP client: one real server process per call, plus ``info``.

Differences from the benchmark client: ``info`` returns the initialize reply
(serverInfo, protocolVersion) together with the spawned server's pid, cwd and
the Asha package path it will import, so the process identity can be recorded
instead of assumed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ASHA_ROOT = os.environ.get("ASHA_HOME", "/home/amir/src/Asha")
PYTHON = os.environ.get("ASHA_PY", "/home/amir/src/Asha/.venv/bin/python")


def _session(state_dir: str, request: list[dict]) -> tuple[list[dict], dict]:
    env = dict(os.environ)
    env["ASHA_STATE_DIR"] = state_dir
    proc = subprocess.Popen(
        [PYTHON, "-m", "asha.mcp_server"], cwd=ASHA_ROOT,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=env)
    identity = {"pid": proc.pid, "cwd": ASHA_ROOT, "python": PYTHON,
                "argv": [PYTHON, "-m", "asha.mcp_server"]}
    replies = []
    try:
        frames = [{"jsonrpc": "2.0", "id": 0, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18",
                              "capabilities": {},
                              "clientInfo": {"name": "phase11-host",
                                             "version": "1"}}}]
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
    return replies, identity


def main(argv: list[str]) -> int:
    state = os.environ.get("ASHA_BENCH_STATE")
    if not state:
        print("ASHA_BENCH_STATE is not set", file=sys.stderr)
        return 2
    mode = argv[1] if len(argv) > 1 else "list"
    if mode == "info":
        replies, identity = _session(state, [{"method": "tools/list",
                                              "params": {}}])
        print(json.dumps({"identity": identity,
                          "initialize": replies[0].get("result"),
                          "tools": [t["name"] for t in
                                    replies[1]["result"]["tools"]]},
                         indent=1))
        return 0
    if mode == "list":
        replies, _ = _session(state, [{"method": "tools/list", "params": {}}])
        print(json.dumps({"tools": [t["name"] for t in replies[1]["result"]["tools"]]}))
        return 0
    if mode == "call" and len(argv) >= 4:
        arguments = json.loads(argv[3])
        replies, _ = _session(state, [{"method": "tools/call",
                                       "params": {"name": argv[2],
                                                  "arguments": arguments}}])
        result = replies[1]["result"]
        print(result["content"][0]["text"] if result.get("content") else json.dumps(result))
        return 0 if result.get("isError") is False else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
