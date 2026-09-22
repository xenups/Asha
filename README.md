# Hermes Disciplined Harness

A standalone, fail-closed toolchain for reproducible agent work: J-Space
governance, AST-based perception, and atomic in-situ execution — with
**empirical, machine-measured benchmarks** and an OS-agnostic bootstrap so the
same harness can be rebuilt identically on Linux, macOS, and Windows.

Everything in `Benchmarks` below was measured on a real machine (see
`Environment` for the exact matrix); no number is estimated.

---

## 1. Architectural Blueprint

### J-Space Governance (`.jspace/control.py`)

A stdlib-only cooperative controller that keeps the working session in a
durable, human-readable ledger (`.jspace/control.json` + `.jspace/CONTROL.md`).

- **Mandatory `--transport <ssh|local>` gate (fail-closed).** Every invocation
  must declare its execution transport. Omission prints
  `TRANSPORT GATE: --transport <ssh|local> is MANDATORY` to stderr and exits
  **1** *before any state is written* (zero ledger mutation on gate failure).
- **Transport pinning.** The declared transport is recorded in the ledger.
  A later invocation with a different transport is refused
  (`Transport mismatch: ledger has 'local' but CLI passed 'ssh'`) — a session
  cannot silently mix SSH and local execution.
- **Ledger authority.** `control.json` is the single source of truth; command
  order matters (`init` → `pulse`/`checkpoint`/`report` → `check --stage ship`),
  evidence is content-addressed (SHA-256), and cross-phase gates (`check`,
  `audit`) re-verify evidence hashes still match before passing.
- **Zero daemons.** Pure file-based state; no ports, no background process.

### AST Perception (`.hermes/tools/code_search.py`)

tree-sitter-based structural navigation instead of raw file reads:

- `--outline <file>` → symbol declarations only (classes, defs, decorators,
  line ranges, nesting), **no bodies**.
- `--pattern <pat> --file <f>` → ast-grep AST pattern matches (line/col ranges
  + snippets only).
- `--trace <symbol> --dir <d>` → import / call / inherit impact map across a
  tree, compact `{file, line, usage_type}` entries.
- `--verify-env` → fail-closed dependency pin check (exact-version comparison,
  exit 1 with a fix hint, **never auto-installs**).

### Atomic In-situ Execution (`.hermes/tools/diff_engine.py`)

Aider-style atomic SEARCH/REPLACE patching, local disk only:

- SEARCH block must match the target **exactly** and **uniquely**; missing or
  ambiguous blocks raise `ValueError` → CLI exit 1, target untouched.
- **Atomic write**: patched content goes to a temp sibling, integrity-verified,
  then `os.replace` — a crash between write and replace leaves the original
  file intact (zero corruption window).
- Rollback = the inverse SEARCH/REPLACE hunk; verified in tests and measured
  below.

### Operating Invariants

1. No edit without `--transport` declared (gate exits 1).
2. Ledger is the authority; every phase gate re-verifies evidence hashes.
3. Patching is atomic-only — SEARCH/REPLACE or nothing; no partial writes.
4. Tools are zero-daemon: no listening ports are ever spawned.

---

## 2. Tooling & Environment Catalog (Reproducibility Matrix)

Everything below was used to produce this repository and its measured numbers.

### Python runtime

| Component | Version (measured) |
|---|---|
| CPython interpreter | 3.11.16 (Windows, x86-64) |
| venv location | `.hermes/venv/` (git-ignored) |
| core lint/type/test | ruff 0.16.8 · mypy 2.3.1 · pytest 9.1.1 |
| vector memory stack | chromadb 1.5.9 · mem0ai 2.1.0 |

### Pinned ABI-critical parser matrix

| Package | Pinned version |
|---|---|
| `tree-sitter` | **0.21.3** |
| `tree-sitter-languages` | **1.10.2** |
| `ast-grep-py` | **0.45.3** |

**Why the pins matter — the 2-argument constructor ABI hazard:**
`tree-sitter` **0.24+** changed its core `Parser` C ABI: the constructor
`Language.???`/`Parser()` bindings switched to a **2-argument calling
convention** (language + options) and the internal C struct layout changed.
`tree-sitter-languages==1.10.2` was compiled against the **0.21.x ABI**
(`get_parser(lang)` → `Parser(language)` single-argument). Mixing a pinned
parser wheel with a newer core (`>=0.24`) causes a **segfault or
`TypeError: Parser.__init__() takes 1 positional argument but 2 were given`**
at first parse — a silent, non-Pythonic crash. Version 0.21.3 + 1.10.2 +
0.45.3 are verified together by `code_search.py --verify-env` (exact-string
comparison; mismatch → exit 1). `ast-grep-py==0.45.3` additionally pins the
parser ABI for `SgRoot(src, lang)` pattern matching. **Do not upgrade one
without re-verifying all three.**

### MCP server declarations (stdio only)

| Server | Package | Transport |
|---|---|---|
| `sequential_thinking` | `npx -y @modelcontextprotocol/server-sequential-thinking` | `stdio` |
| `remote_linux` | `@modelcontextprotocol/server-ssh` (SSH MCP) | `stdio` |

Both run strictly over `stdio` — no TCP ports, no HTTP listeners, matching the
zero-daemon invariant. `remote_linux` is the *only* sanctioned SSH pathway;
when used, every `control.py` invocation on the remote side must declare
`--transport ssh` (see §1).

### In-situ local utilities

| Tool | Role |
|---|---|
| `.hermes/tools/code_search.py` | AST outline / pattern / impact trace |
| `.hermes/tools/diff_engine.py` | Atomic SEARCH/REPLACE patch |
| `.jspace/control.py` | Session ledger + transport gate |
| `skills/pre-ship-quality-gate/SKILL.md` | Fail-closed pre-ship audit protocol |

### Repository layout

```
hermes-disciplined-harness/
├── .hermes/
│   ├── venv/                  # git-ignored; see Bootstrap
│   └── tools/
│       ├── code_search.py     # AST perception
│       └── diff_engine.py     # atomic patching
├── .jspace/
│   ├── control.py             # governance + transport gate
│   ├── control.json           # runtime ledger (git-ignored)
│   └── cache/                 # git-ignored
├── skills/pre-ship-quality-gate/SKILL.md
├── tests/                     # 15 regression tests (14 pass, 1 env-probe skip)
├── benchmarks/
│   ├── bench.py               # empirical benchmark runner (re-runnable)
│   └── results.json           # machine-measured numbers for this README
├── ruff.toml                  # centralized lint exceptions
└── .gitignore
```

---

## 3. Cross-Platform Bootstrap (OS-Agnostic)

Recreates the identical `.hermes/venv` + toolchain from scratch. Requires only
a Python ≥ 3.10 with `venv` and a shell. **Copy-paste, both platforms.**

### Linux / macOS (bash)

```bash
git clone <your-repo-url> hermes-disciplined-harness
cd hermes-disciplined-harness
python3 -m venv .hermes/venv
source .hermes/venv/bin/activate
pip install --upgrade pip
pip install \
  "tree-sitter==0.21.3" \
  "tree-sitter-languages==1.10.2" \
  "ast-grep-py==0.45.3" \
  "ruff" "mypy" "pytest" "chromadb" "mem0ai"
python .hermes/tools/code_search.py --verify-env   # must print ENV CHECK OK
python .hermes/tools/code_search.py --self-test
python .hermes/tools/diff_engine.py --self-test
python -m pytest tests/ -q
```

### Windows (PowerShell)

```powershell
git clone <your-repo-url> hermes-disciplined-harness
cd hermes-disciplined-harness
py -3 -m venv .hermes\venv
.hermes\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install "tree-sitter==0.21.3" "tree-sitter-languages==1.10.2" "ast-grep-py==0.45.3" "ruff" "mypy" "pytest" "chromadb" "mem0ai"
python .hermes\tools\code_search.py --verify-env
python .hermes\tools\code_search.py --self-test
python .hermes\tools\diff_engine.py --self-test
python -m pytest tests\ -q
```

### Windows (CMD)

```cmd
git clone <your-repo-url> hermes-disciplined-harness
cd hermes-disciplined-harness
py -3 -m venv .hermes\venv
.hermes\venv\Scripts\python.exe -m pip install --upgrade pip
.hermes\venv\Scripts\python.exe -m pip install "tree-sitter==0.21.3" "tree-sitter-languages==1.10.2" "ast-grep-py==0.45.3" "ruff" "mypy" "pytest" "chromadb" "mem0ai"
.hermes\venv\Scripts\python.exe .hermes\tools\code_search.py --verify-env
.hermes\venv\Scripts\python.exe .hermes\tools\diff_engine.py --self-test
.hermes\venv\Scripts\python.exe -m pytest tests -q
```

**Drift check:** after bootstrap, both platforms must pass all gates listed in
§5. The venv path separator is the only platform difference; the toolchain
itself is byte-identical.

---

## 4. Empirical Benchmarks

**Environment (Windows 11, git-bash/MSYS, x86-64):** CPython 3.11.16, ruff
0.16.8, mypy 2.3.1, pytest 9.1.1, tree-sitter 0.21.3, tree-sitter-languages
1.10.2, ast-grep-py 0.45.3. Benchmark subject: `.jspace/control.py`
(767 lines / 3,087 tokens — a real, shipped file, not a synthetic fixture).
Rerun anytime: `.hermes/venv/python benchmarks/bench.py --json`.

| Benchmark | Metric | Measured value |
|---|---|---|
| **AST outline vs raw read** | Raw source | 767 lines · 3,087 tokens |
| | AST outline | 39 lines · 117 tokens |
| | **Token reduction** | **96.21 %** |
| | Parse latency (median, n=5) | **154.78 ms** |
| **Atomic diff safety** | Valid patch apply (median, n=5) | **111.89 ms** |
| | Rollback via inverse hunk (median, n=5) | **106.42 ms** |
| | Colliding/mismatched patch | **exit code 1** · 104.13 ms · **zero corruption** (bytes identical, no temp litter) |
| **Fail-closed transport gate** | `init` without `--transport` | **exit code 1** · **no ledger written** |
| | `init --transport local` | **exit code 0** · ledger pinned `transport: local` |
| | Mixed transport (`ssh` on a `local` session) | **exit code 1** (`Transport mismatch`) |
| **Zero-daemon verification** | Listening TCP ports before tool runs | 38 |
| | Listening TCP ports after tool runs | 38 |
| | New listeners spawned | **0** |

Sanity: the 96.21 % token reduction is exactly the outline's job — 3,087
tokens of bodies and strings collapse to 117 tokens of symbol declarations
with line ranges, at 154.78 ms median parse on this machine. Both parse
latencies are dominated by cold venv interpreter startup (the stdlib tools
themselves run in low single-digit ms); `samples_ms` arrays are in
`benchmarks/results.json` if you need the distribution.

---

## 5. Universal Target-Agnostic Operating Policy

Applies to any repository or agent session using this harness — the tools are
target-agnostic by design (they operate on files and ledgers, not one codebase).

1. **Mandatory transport flag.** Every `control.py` invocation carries
   `--transport <ssh|local>`. Omission or an unknown value ⇒ exit 1, no state
   written. Remote (SSH) execution declares `ssh`; everything else `local`.
   A session's transport is pinned in the ledger; mixing is refused.
2. **J-Space ledger authority.** `.jspace/control.json` is the single source
   of truth for goals, next actions, checkpoints (SHA-256 evidence receipts)
   and reports. Never hand-edit it; use `control.py`. A checked-in ledger is
   a contradiction (git-ignored); the *policy* ships, the *session state* does
   not.
3. **Atomic-only patching invariant.** All file edits go through
   `diff_engine.py` SEARCH/REPLACE (exact, unique match → temp + verify →
   `os.replace`). Any patch that cannot match exactly is rejected whole —
   no partial application, no partial writes, ever.
4. **Pre-ship quality gate (fail-closed).** Before commit/push:
   `ruff check .` exit 0, `mypy .` exit 0, `pytest` green, and (when a session
   is active) `control.py --transport <t> check --stage ship` must print
   `GATE SHIP: PASS`. Any red gate ⇒ not shippable; fix and re-run.
5. **Zero-daemon.** Tools spawn no servers, no listeners, no background
   processes. Verify with `netstat`/`ss` (measurement: 0 new ports, §4).

---

## Verified gates (as produced)

```
ruff check .            → All checks passed!        (exit 0)
mypy  .                 → Success: no issues found  (exit 0)
pytest tests/ -q        → 14 passed, 1 skipped      (exit 0; skip = env-probe)
code_search --self-test → PASSED
diff_engine --self-test → PASSED
benchmarks/bench.py     → all four benchmarks measured, results.json written
```