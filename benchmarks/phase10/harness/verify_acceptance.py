#!/usr/bin/env python3
"""Independent acceptance verification for the p10 benchmark.

Runs inside a worktree's pydetactl directory with the pinned interpreter:

    cd <worktree>/pydetactl
    env -u VIRTUAL_ENV /home/amir/src/detailist/pydetactl/.venv/bin/python \
        /home/amir/bench/p10/verify_acceptance.py <T1|T2|T3>

Prints one JSON line. Nothing is imported from the agent's report.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys

sys.path.insert(0, "src")


def t1() -> dict:
    from utils.s3_resolver import detect_s3_urls

    c1 = detect_s3_urls({"m": "fetched https://host/a/s3/manifest.json ok"})
    c2 = detect_s3_urls("see s3://b/k.txt).")
    return {
        "c1_value": c1,
        "c1_pass": c1 == ["https://host/a/s3/manifest.json"],
        "c2_value": c2,
        "c2_pass": c2 == ["s3://b/k.txt"],
    }


def t2() -> dict:
    from unittest.mock import patch

    from utils.s3_resolver import resolve_s3_artifacts

    good = {"source": "s3://b/a.yaml", "content": {"a": 1}}
    out = io.StringIO()
    err = io.StringIO()
    calls = []

    async def fake_fetch(self, url):
        calls.append(url)
        if url == "s3://b/bad.yaml":
            raise RuntimeError("boom")
        return good

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def fetch_artifact(self, url):
            return await fake_fetch(self, url)

    with patch("utils.s3_resolver.S3Client", FakeClient), \
            patch("builtins.print",
                  side_effect=lambda *a, **k: calls.append(("print", a))):
        stdout_ctx, stderr_ctx = contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err)
        with stdout_ctx, stderr_ctx:
            data = {"a": "s3://b/a.yaml", "b": "s3://b/bad.yaml"}
            artifacts = asyncio.run(resolve_s3_artifacts(data))

    printed = [c for c in calls if isinstance(c, tuple) and c[0] == "print"]
    return {
        "stdout_is_empty": out.getvalue() == "",
        "stdout_repr": out.getvalue()[:120],
        "builtins_print_used": bool(printed),
        "non_fatal": [a["source"] for a in artifacts] == ["s3://b/a.yaml"],
        "artifact_count": len(artifacts),
        "reported_on_stderr": "boom" in err.getvalue(),
    }


def t3() -> dict:
    from services.s3_client import S3Client
    from utils.s3_resolver import detect_s3_urls

    corpus = [
        "s3://bucket/key.yaml",
        "s3://bucket/dir/key.json",
        "https://host/a/s3/manifest.json",
        "http://minio.s3.local/b/k.txt",
        "see s3://b/k.txt). and https://h/x/s3/y.json ok",
        {"nested": ["s3://b1/k1", {"u": "https://h/s3/k2"}]},
        "no urls here at all",
    ]
    found: list[str] = []
    for item in corpus:
        found.extend(detect_s3_urls(item))
    client = S3Client.__new__(S3Client)
    unparseable = []
    for url in found:
        try:
            bucket, key = client._parse_s3_url(url)
        except Exception as exc:  # noqa: BLE001 - the failure itself is the datapoint
            unparseable.append([url, repr(exc)])
            continue
        if not bucket or not key:
            unparseable.append([url, "empty bucket/key"])
    pre_existing = client._parse_s3_url("s3://bucket/key.yaml")
    return {
        "detected": sorted(found),
        "unparseable": unparseable,
        "property_holds": not unparseable,
        "client_contract": list(pre_existing) == ["bucket", "key.yaml"],
    }


CHECKS = {"T1": t1, "T2": t2, "T3": t3}

if __name__ == "__main__":
    task = sys.argv[1] if len(sys.argv) > 1 else ""
    if task not in CHECKS:
        print(json.dumps({"error": f"unknown task {task!r}"}))
        raise SystemExit(2)
    try:
        result = CHECKS[task]()
    except Exception as exc:  # noqa: BLE001 - report, never crash the harness
        result = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps({"task": task, "result": result}))
