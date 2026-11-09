"""Observability layer: event model, store, HTTP API, SSE, MCP purity."""
from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from asha import mcp_server
from asha.observability import events as obs
from asha.observability import server as dash
from asha.observability import store


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASHA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv(obs.ENV_ENABLE, "1")
    obs.reset()
    yield
    obs.reset()


def _root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return repo


def _record(tmp_path: Path, count: int = 3, run_id: str = "run-1") -> Path:
    root = _root(tmp_path)
    obs.start_run(root, run_id, {"command": "test", "pid": 999999})
    for i in range(count):
        obs.emit("worker.started", "test", worker_id=f"w{i % 2}",
                 status="RUNNING", message=f"tick {i}",
                 data={"reads": ["a.py"], "writes": ["b.py"]})
    return root


def test_disabled_by_default_is_a_noop(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(obs.ENV_ENABLE, raising=False)
    obs.reset()
    root = _root(tmp_path)
    obs.start_run(root, "run-off", {})
    obs.emit("worker.started", "test", worker_id="w1")
    assert not obs.enabled()
    assert not store.runs_dir(root).exists()


def test_events_are_versioned_and_sequential(tmp_path) -> None:
    root = _record(tmp_path)
    rows = store.read_events(root, "run-1")
    assert [r["sequence"] for r in rows] == [1, 2, 3]
    for row in rows:
        assert row["schema_version"] == 1
        assert row["run_id"] == "run-1"
        assert row["timestamp"].endswith("Z")
        assert row["event_type"] == "worker.started"
        assert row["component"] == "test"


def test_cursor_replay(tmp_path) -> None:
    root = _record(tmp_path, count=5)
    assert [r["sequence"] for r in
            store.read_events(root, "run-1", after=2)] == [3, 4, 5]
    assert store.read_events(root, "run-1", after=99) == []


def test_concurrent_emission_keeps_one_sequence(tmp_path) -> None:
    root = _root(tmp_path)
    obs.start_run(root, "run-c", {})

    def blast(worker: str) -> None:
        for i in range(25):
            obs.emit("log.info", "test", worker_id=worker,
                     message=f"{worker}-{i}")

    threads = [threading.Thread(target=blast, args=(f"w{i}",))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seqs = [r["sequence"] for r in store.read_events(root, "run-c")]
    assert len(seqs) == 200
    assert len(set(seqs)) == 200
    assert seqs == sorted(seqs)


def test_secrets_are_redacted(tmp_path) -> None:
    root = _root(tmp_path)
    obs.start_run(root, "run-r", {})
    obs.emit("log.error", "test", message="api_key=SECRETVALUE boom",
             data={"password": "hunter2", "note": "token=abc123"})
    text = (store.run_path(root, "run-r") / "events.jsonl").read_text("utf-8")
    assert "SECRETVALUE" not in text
    assert "hunter2" not in text
    assert "abc123" not in text
    assert "[REDACTED]" in text


def test_a_broken_sink_cannot_break_the_engine(tmp_path) -> None:
    class Boom:
        def emit(self, event): raise RuntimeError("disk on fire")

    obs.configure(Boom(), "run-x")
    obs.emit("worker.started", "test", worker_id="w1")  # must not raise
    obs.emit("worker.started", "test", worker_id="w2")
    assert not obs.enabled()


def test_malformed_records_are_skipped(tmp_path) -> None:
    root = _record(tmp_path, count=2)
    path = store.run_path(root, "run-1") / "events.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n\n")
    assert [r["sequence"] for r in store.read_events(root, "run-1")] == [1, 2]


def test_invalid_run_id_is_rejected(tmp_path) -> None:
    root = _root(tmp_path)
    for bad in ("../etc", "a/b", "", "x" * 200, ".hidden"):
        with pytest.raises(ValueError):
            store.read_events(root, bad)


def test_snapshot_reports_unknown_rather_than_success(tmp_path) -> None:
    root = _record(tmp_path, count=2)
    snap = store.snapshot(root, "run-1")
    assert snap["status"] == "INTERRUPTED"  # pid 999999 is not alive
    assert snap["workers"]["w0"]["state"] == "RUNNING"
    assert snap["dag"]["available"] is False
    assert snap["codegraph"]["state"] == "UNKNOWN"
    assert snap["verdict"] is None


def test_snapshot_derives_dag_and_terminal_state(tmp_path) -> None:
    root = _root(tmp_path)
    obs.start_run(root, "run-d", {"pid": 999999})
    obs.emit("dag.created", "dag", stage_id="dag", status="created",
             data={"nodes": [{"id": "w1", "deps": []}],
                   "edges": {"w1": []}, "generation": 0})
    obs.emit("worker.completed", "dag", stage_id="worker", status="DONE",
             worker_id="w1")
    obs.finish_run("ok", reason=None)
    snap = store.snapshot(root, "run-d")
    assert snap["status"] == "ok"
    assert snap["dag"]["nodes"][0]["id"] == "w1"
    assert snap["workers"]["w1"]["state"] == "DONE"


# ------------------------------------------------------------------ HTTP


@pytest.fixture()
def http(tmp_path: Path):
    root = _record(tmp_path, count=4)
    obs.finish_run("ok")
    httpd = dash.make_server(root, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, root
    httpd.shutdown()
    httpd.server_close()


def _get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as res:
        return res.status, dict(res.headers), res.read()


def test_health_and_run_listing(http) -> None:
    base, _ = http
    status, _, body = _get(base + "/api/health")
    assert status == 200 and json.loads(body)["ok"] is True
    status, _, body = _get(base + "/api/runs")
    runs = json.loads(body)["runs"]
    assert [r["run_id"] for r in runs] == ["run-1"]
    assert runs[0]["status"] == "ok"


def test_snapshot_graph_and_events_endpoints(http) -> None:
    base, _ = http
    _, _, body = _get(base + "/api/runs/run-1")
    snap = json.loads(body)
    assert snap["run_id"] == "run-1"
    assert snap["sequence"] == 5  # 4 events + terminal
    _, _, body = _get(base + "/api/runs/run-1/graph")
    assert set(json.loads(body)) == {"dag", "codegraph"}
    _, _, body = _get(base + "/api/runs/run-1/events?after=3")
    assert [e["sequence"] for e in json.loads(body)["events"]] == [4, 5]


def test_unknown_and_malformed_run_ids(http) -> None:
    base, _ = http
    with pytest.raises(urllib.error.HTTPError) as missing:
        _get(base + "/api/runs/does-not-exist")
    assert missing.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as bad:
        _get(base + "/api/runs/..%2f..%2fetc")
    assert bad.value.code in (400, 404)
    with pytest.raises(urllib.error.HTTPError) as traversal:
        _get(base + "/../../etc/passwd")
    assert traversal.value.code == 404


def test_security_headers_and_static_assets(http) -> None:
    base, _ = http
    status, headers, body = _get(base + "/")
    assert status == 200 and b"<!DOCTYPE html>" in body
    assert headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    for asset in ("/app.js", "/styles.css"):
        status, _, body = _get(base + asset)
        assert status == 200 and body


def test_sse_replay_then_live_delivery(http) -> None:
    base, _root = http
    res = urllib.request.urlopen(base + "/api/stream?run_id=run-1", timeout=5)
    frames: list[str] = []
    try:
        for _ in range(10):
            line = res.readline().decode()
            if line.startswith("data: "):
                frames.append(line)
            if len(frames) >= 2:
                break
    finally:
        res.close()
    assert len(frames) >= 2
    first = json.loads(frames[0][6:])
    assert first["sequence"] == 1
    # reconnect with a cursor: only later events are replayed
    res = urllib.request.urlopen(
        base + "/api/stream?run_id=run-1&after=4", timeout=5)
    try:
        payload = None
        for _ in range(10):
            line = res.readline().decode()
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                break
    finally:
        res.close()
    assert payload is not None and payload["sequence"] == 5


# ------------------------------------------------------------- MCP purity


def test_mcp_stdout_stays_jsonrpc_with_telemetry_on(tmp_path) -> None:
    stdin = io.StringIO(
        '{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}\n'
        'NOT JSON\n'
        '{"jsonrpc": "2.0", "id": 2, "method": "ping"}\n')
    stdout, stderr = io.StringIO(), io.StringIO()
    assert mcp_server.serve(stdin, stdout, stderr) == 0
    lines = [line for line in stdout.getvalue().splitlines() if line]
    assert lines
    for line in lines:
        json.loads(line)  # every line is one frame
    assert "Traceback" not in stdout.getvalue()


def test_dashboard_cli_refuses_public_bind() -> None:
    from asha.cli import main
    assert main(["dashboard", "--host", "0.0.0.0", "--port", "0"]) == 2


def test_run_scope_never_writes_inside_the_repository(tmp_path) -> None:
    root = _record(tmp_path)
    assert not (root / "observability").exists()
    assert store.runs_dir(root).is_dir()
    time.sleep(0.01)
