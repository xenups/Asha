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

Expect `SMOKE: OK` (exit 0). The live line reports `SKIPPED` when the
live flag/key are absent — that is an explicit skip, **not** a pass; it
turns into one real bounded request when configured, and a failure there
exits 1.

## 4. Running the services

```bash
asha --help                      # CLI (also: python -m asha)
python -m asha.mcp_server        # MCP server: stdio JSON-RPC on stdin/stdout
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
