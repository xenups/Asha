"""Observability layer: event model, store, HTTP API, SSE, MCP purity."""
from __future__ import annotations

import io
import json
import subprocess
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

# -- Phase 15: F1 oversized events and F2 CodeGraph root validation ----------


def _dag_data(workers: int) -> dict:
    return {
        "nodes": [{"id": f"w{i}", "task": f"build module {i}",
                   "reads": [f"src/m{i}.py"], "writes": [f"src/m{i}.py"],
                   "declared_scope": [f"src/m{i}.py"],
                   "deps": [] if i == 0 else [f"w{i - 1}"]}
                  for i in range(workers)],
        "edges": {f"w{i}": ([] if i == 0 else [f"w{i - 1}"])
                  for i in range(workers)},
        "generation": workers,
        "uncertain": [],
    }


def _emit_dag(run_root: Path, workers: int, run_id: str) -> tuple[Path, str]:
    obs.start_run(run_root, run_id, {"command": "dag", "workers": workers})
    obs.emit("dag.created", "dag_coordinator", stage_id="dag",
             status="created", message="worker DAG projected",
             data=_dag_data(workers))
    obs.emit("worker.started", "test", worker_id="w0", status="RUNNING",
             message="tick")
    obs.finish_run("ok")
    return run_root, run_id


def _every_line_parses(run_root: Path, run_id: str) -> list[dict]:
    path = obs.run_dir(run_root) / "runs" / run_id / "events.jsonl"
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_large_dag_event_sixty_workers_is_persisted(tmp_path: Path) -> None:
    root, run_id = _emit_dag(_root(tmp_path), 60, "dag-60")
    records = _every_line_parses(root, run_id)
    dag = next(r for r in records if r["event_type"] == "dag.created")
    assert len(dag["data"]["nodes"]) == 60
    assert dag["data"]["nodes"][0]["id"] == "w0"
    assert dag["data"]["edges"]["w59"] == ["w58"]
    assert dag["data"].get("truncation") is None
    replayed = store.read_events(root, run_id, limit=10)
    assert replayed[0]["event_type"] == "dag.created"


def test_large_dag_event_hundred_workers_is_persisted(tmp_path: Path) -> None:
    root, run_id = _emit_dag(_root(tmp_path), 100, "dag-100")
    records = _every_line_parses(root, run_id)
    dag = next(r for r in records if r["event_type"] == "dag.created")
    assert len(dag["data"]["nodes"]) == 100
    assert len(json.dumps(dag).encode("utf-8")) <= obs.MAX_EVENT_BYTES


def test_oversized_dag_is_pruned_and_marked(tmp_path: Path) -> None:
    root, run_id = _emit_dag(_root(tmp_path), 600, "dag-600")
    records = _every_line_parses(root, run_id)
    dag = next(r for r in records if r["event_type"] == "dag.created")
    cut = dag["data"]["truncation"]
    assert cut["truncated"] is True
    assert cut["omitted"]["nodes"] > 0
    assert cut["omitted"]["edges"] > 0
    ids = {node["id"] for node in dag["data"]["nodes"]}
    assert ids
    assert ids <= {f"w{i}" for i in range(600)}
    for key, deps in dag["data"]["edges"].items():
        assert key in ids
        assert all(dep in ids for dep in deps)


def test_codegraph_event_with_many_nodes_stays_readable(tmp_path: Path) -> None:
    root = _root(tmp_path)
    run_id = "graph-200"
    obs.start_run(root, run_id, {"command": "dag"})
    nodes = [{"id": f"pkg.mod{i}.run", "kind": "module",
              "module": f"pkg.mod{i}", "confidence": "COMPLETE"}
             for i in range(200)]
    relations = [{"from": nodes[i]["id"], "to": nodes[i + 1]["id"],
                  "rel": "import"} for i in range(len(nodes) - 1)]
    obs.emit("codegraph.load.completed", "codegraph", stage_id="codegraph",
             status="completed", message="graph warm_hit",
             data={"state": "COMPLETE", "source": "warm_hit", "modules": 200,
                   "edges": len(relations), "nodes": nodes,
                   "relations": relations, "unresolved": 0})
    obs.finish_run("ok")
    records = _every_line_parses(root, run_id)
    graph = next(r for r in records if r["event_type"].startswith("codegraph"))
    assert graph["data"]["nodes"]
    if graph["data"].get("truncation"):
        assert graph["data"]["state"] == "INCOMPLETE"
        kept = {node["id"] for node in graph["data"]["nodes"]}
        assert all(rel["from"] in kept and rel["to"] in kept
                   for rel in graph["data"]["relations"])


def test_multibyte_payload_measures_utf8_bytes(tmp_path: Path) -> None:
    root = _root(tmp_path)
    run_id = "utf8"
    obs.start_run(root, run_id, {"command": "dag"})
    filler = "فایل‌های پروژه با نام بسیار طولانی " * 12  # 2 bytes per char
    obs.emit("log.info", "test", message="شروع بررسی",
             data={"files": [f"{filler}{i}.py" for i in range(150)]})
    obs.emit("log.info", "test", message="پایان")
    obs.finish_run("ok")
    records = _every_line_parses(root, run_id)
    for record in records:
        assert len(json.dumps(record, ensure_ascii=False).encode("utf-8"))             <= obs.MAX_EVENT_BYTES
    big = records[0]
    assert big["message"] == "شروع بررسی"
    assert big["data"].get("truncation") or big["data"].get("reason")


def test_replay_integrity_across_large_and_small_events(tmp_path: Path) -> None:
    root, run_id = _emit_dag(_root(tmp_path), 600, "mix")
    obs.emit("log.info", "test", message="after the big one")
    obs.finish_run("ok")
    records = _every_line_parses(root, run_id)
    sequences = [record["sequence"] for record in records]
    assert sequences == list(range(1, len(records) + 1))
    assert len(set(sequences)) == len(sequences)
    assert [r["event_type"] for r in records][-1] == "run.completed"
    assert len(store.read_events(root, run_id, limit=100)) == len(records)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True)
    (path / "a.py").write_text("x = 1\n", encoding="utf-8")
    return path


def test_project_graph_rejects_an_empty_root(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    dash._GRAPH_MEMO.clear()
    monkeypatch.chdir(_git_repo(tmp_path / "cwd"))
    assert dash._project_graph("") == {
        "available": False, "state": "UNKNOWN",
        "reason": "no repository root in run metadata"}


def test_project_graph_rejects_a_missing_path(tmp_path: Path) -> None:
    dash._GRAPH_MEMO.clear()
    out = dash._project_graph(str(tmp_path / "absent"))
    assert out["available"] is False
    assert out["state"] == "UNKNOWN"
    assert "not a directory" in out["reason"]


def test_project_graph_rejects_a_regular_file(tmp_path: Path) -> None:
    dash._GRAPH_MEMO.clear()
    target = tmp_path / "plain.txt"
    target.write_text("x", encoding="utf-8")
    out = dash._project_graph(str(target))
    assert out["available"] is False
    assert "not a directory" in out["reason"]


def test_project_graph_rejects_a_non_git_directory(tmp_path: Path) -> None:
    dash._GRAPH_MEMO.clear()
    plain = tmp_path / "plain"
    plain.mkdir()
    out = dash._project_graph(str(plain))
    assert out["available"] is False
    assert "not a git repository" in out["reason"]


def test_project_graph_never_uses_the_working_directory(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    dash._GRAPH_MEMO.clear()
    cwd_repo = _git_repo(tmp_path / "cwd")
    (cwd_repo / "other.py").write_text("y = 2\n", encoding="utf-8")
    monkeypatch.chdir(cwd_repo)
    for bad in ("", str(tmp_path / "absent"), str(tmp_path)):
        out = dash._project_graph(bad)
        assert out["available"] is False
        assert out["state"] == "UNKNOWN"
        assert "nodes" not in out


def test_project_graph_accepts_a_valid_repository(tmp_path: Path) -> None:
    dash._GRAPH_MEMO.clear()
    repo = _git_repo(tmp_path / "repo")
    out = dash._project_graph(str(repo))
    assert out["available"] is True
    assert out["state"] in ("COMPLETE", "INCOMPLETE")
    assert out["nodes"]


def test_project_graph_accepts_a_linked_worktree(tmp_path: Path) -> None:
    dash._GRAPH_MEMO.clear()
    repo = _git_repo(tmp_path / "repo")
    (repo / "b.py").write_text("z = 3\n", encoding="utf-8")
    git = ["git", "-C", str(repo), "-c", "user.email=a@b.c",
           "-c", "user.name=audit"]
    subprocess.run([*git, "add", "."], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True,
                   capture_output=True)
    worktree = tmp_path / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q",
                    str(worktree), "-b", "side"], check=True,
                   capture_output=True)
    assert (worktree / ".git").is_file()
    out = dash._project_graph(str(worktree))
    assert out["available"] is True
    assert out["nodes"]
