# Asha on a Debian/Ubuntu Server — Deployment & Smoke Guide

Target: self-hosted Debian/Ubuntu node running the Asha CLI, the MCP
server (stdio JSON-RPC), and the live LLM semantic reviewer. Asha itself
is **stdlib-only** (Python >= 3.11, zero runtime dependencies); git is
required (snapshot + scope resolution shell out to it).

## 1. Clone + venv + install

```bash
sudo apt update && sudo apt install -y git python3 python3-venv python3-pip
git clone https://github.com/xenups/Asha.git && cd Asha
python3 -m venv .venv
. .venv/bin/activate
pip install -e .            # console scripts: asha, asha-mcp
python scripts/check_env.py --profile core   # offline parity check, exit 0 = OK
```

Optional feature extras (same as CI): `pip install -e '.[code-search]'`
for tree-sitter search, `pip install -e '.[memory]'` for mem0/Chroma.

## 2. Environment

Copy the template and fill it in, then source it (nothing loads `.env`
automatically — stdlib only):

```bash
cp .env.example .env && $EDITOR .env
set -a; . ./.env; set +a
```

| Variable | Meaning | Default |
|---|---|---|
| `ASHA_SEMANTIC_REVIEW=live` | enables the live reviewer (omit to keep it disabled) | off |
| `ASHA_LLM_API_KEY` | API key — required when live, never defaulted | — |
| `ASHA_LLM_BASE_URL` | OpenAI-compatible base URL | `https://api.deepseek.com` |
| `ASHA_LLM_MODEL` | chat model | `deepseek-chat` |
| `ASHA_LLM_TIMEOUT_S` / `ASHA_LLM_MAX_TOKENS` / `ASHA_LLM_TEMPERATURE` | per-request budget / spend cap / sampling | 120 / 4096 / 0.0 |
| `ASHA_REVIEW_CONTEXT_BUDGET` | prompt context chars per review | 60000 |
| `ASHA_STATE_DIR`, `ASHA_GRAPH_CACHE_DIR` | state / graph cache locations | per `asha/common/paths.py` |

Fail-closed contract: with live enabled and any transport/config/parse
failure, the gate records `EXECUTION_FAILED` (deny); with live disabled
it records `INVALID_EVIDENCE` on the missing-artifact path (deny). There
is no mock fallback — a reviewer can only enter through config, never
implicitly.

## 3. Smoke test (snapshot hashing + gate + live connectivity)

```bash
python scripts/verify_linux_node.py
```

Expect `SMOKE: OK` (exit 0). Run it after every clone, venv upgrade, or
`git pull` to confirm baseline node health (snapshot hashing + gate +
optional live connectivity). The live line reports `SKIPPED` when the
live flag/key are absent — that is an explicit skip, **not** a pass; it
turns into one real bounded request when configured, and a failure there
exits 1.

## 4. Running the services

```bash
asha --help                      # CLI (also: python -m asha)
python -m asha.mcp_server        # MCP server: stdio JSON-RPC on stdin/stdout
```

One-shot CLI (Phase 9) — the server entry point doubles as a direct
tool driver; a bare positional path maps to the repository root:

```bash
asha status /path/to/repo                # deterministic tip one-liner
asha status --json /path/to/repo         # exactly one JSON document
asha-mcp asha_status /path/to/repo       # positional PATH == root=PATH
asha-mcp asha_verify_tip /path/to/repo run_checks=true
asha-mcp asha_verify_tip /path/to/repo declared_scope=@diff base_ref=origin/main
asha-mcp asha_verify_stack feat1,feat2,feat3 base_ref=origin/dev /path/to/repo
# array fields are comma-separated in one-shot form; lists arrive as
# JSON arrays over MCP: {"branches": ["feat1", "feat2"], "base_ref": ...}
# exit codes: 0 tool ok / 1 tool refused (isError) / 2 usage error
```

For MCP wiring point your client at: command `asha-mcp` (or
`.venv/bin/python -m asha.mcp_server`), same environment as above.

## 5. Tests / CI parity on the node (optional)

```bash
pip install pytest ruff mypy mypy-baseline
ruff check asha/ tests/
mypy asha/ tests/
python -m pytest tests/ -q
```

Notes for Linux servers:

* run as a dedicated user; keep `ASHA_STATE_DIR` on persistent storage;
* put the LLM key in the service unit / secret store, never in git
  (`.env` is git-ignored; `.env.example` holds placeholders only);
* systemd unit sketch: `ExecStart=/opt/Asha/.venv/bin/asha-mcp`
  with `EnvironmentFile=/opt/Asha/.env`, `WorkingDirectory=/opt/Asha`.

## 6. Stacked-PR / multi-branch rebases: verify the tip in place

When the agent owns branch checkouts and `git rebase --onto` directly
(stacked-PR workflows), verify the CURRENT working tree between the
rebase and `git push --force-with-lease` — no sub-worktree is spawned,
so nothing competes with the branch the agent is manipulating:

```bash
asha-mcp asha_verify_tip /path/to/repo base_ref=origin/main run_checks=true
# optional: declared_scope=... is a repeated-key style list via MCP args;
# from JSON-RPC, pass "declared_scope": ["pkg/mod.py"]
```

* refusal -> exit 1 with a stable code and NO artifact written:
  `TIP_DIRTY`, `TIP_CONFLICTED`, `TIP_DIFF_UNCLEAN`,
  `TIP_REBASE_IN_PROGRESS`, `TIP_BASE_UNKNOWN`, `TIP_SCOPE_MISSING`,
  `TIP_CHECKS_FAILED`;
* pass -> `ok: true` with `snapshot_id`, `evidence_sha256`, and the
  sealed artifact at `<state>/verify-tip/<head12>.evidence.json`
  (digest-bound, tamper-evident; `authorized_to_ship` stays `false` —
  this is verification evidence, never ship authority).

Rule: push the tip only after `ok: true`; on any refusal, fix the tree
or finish the git operation first — never force-push over a refusal.

## 7. Phase 10: stack lineage, auto-diff scope, in-place dispatch

`asha_verify_stack` checks an ordered branch stack (bottom-to-top)
against a base ref without spawning worktrees: linear ancestry per
link plus re-verification of each head's sealed verify-tip evidence.
A drifted link returns the exact recovery recipe
`git rebase --onto <parent_sha> <merge_base> <branch>`; `stack_healthy`
is true only when every branch is linear AND sealed.

```bash
asha-mcp asha_verify_stack feat1,feat2,feat3 base_ref=origin/dev /path/to/repo
# report codes: STACK_ANCESTRY_DRIFT, STACK_EVIDENCE_MISSING,
#               STACK_EVIDENCE_TAMPERED
# refusals (exit 1): STACK_EMPTY, BRANCH_NOT_FOUND, BASE_REF_NOT_FOUND
```

`"@diff"` / `"@diff:<base_ref>"` auto-scope (any of `declared_scope`,
`reads`, `writes` in `asha_dispatch_task`, or `declared_scope` in
`asha_verify_tip`) expands to `git diff --name-only <base>...HEAD`;
`reads` grow by the AST dependency closure of those files; protected
governance paths (`.github/`, `.jspace/`) never enter implicitly; an
empty or unresolvable diff is refused as `SCOPE_AUTO_EMPTY` with
nothing executed.

`execution_mode="in_place_atomic"` in `asha_dispatch_task` runs `cmd`
on the CURRENT branch tip (default `"worktree"` is untouched): a dirty
or conflicted tree is refused immediately (`TIP_DIRTY`,
`TIP_CONFLICTED`), observed changes must stay strictly inside
`declared_scope`, and ANY failure (non-zero exit, timeout, scope
violation) triggers `git reset --hard <pre_head>` + `git clean -fd`
back to the exact pre-execution state — zero unsealed residue. Success
seals digest-bound evidence with `authorized_to_ship: false`.

## 8. Phase 11: unified `asha` entrypoint (one command, one tool)

The unified `asha` MCP tool (registered first, #1 entrypoint) auto-routes
with strict precedence `cmd` > `branches` > `target` > zero-config —
same handlers, same fail-closed contracts. The CLI mirrors each flow:

```bash
asha /path/to/repo          # zero-config: status + sealed tip evidence when
                            # clean; dirty -> ready_to_push: false (TIP_DIRTY)
asha --verify               # in-place tip verification, run_checks=True
asha --stack b1 b2 b3 --base origin/dev
asha --context app.py:foo app2.py:bar
asha --exec "pytest -q"     # in_place_atomic + auto "@diff" scope + rollback
asha-mcp /path/to/repo      # bare path routes to the unified tool
# exit codes: 0 ok / 1 refused-or-failed / 2 usage error
```
