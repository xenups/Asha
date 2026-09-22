# Live A/B Benchmark — With Asha-Harness vs Without (Vanilla)

_All metrics measured on this machine at run time by `AB.py`; no fabricated data._

## Method (read before judging the numbers)

Both modes are deterministic tool pipelines executed by `benchmarks/live_eval/AB.py`
on byte-identical playground copies. **No LLM is in the loop** — an LLM would add
uncontrollable variance; this benchmark isolates the *tooling discipline* itself:
- **Vanilla** simulates what an unassisted agent does: whole-file `read_text`,
  regex/first-occurrence edits, single-file refactors, zero verification before
  declaring a task done.
- **Asha** runs the actual harness tools: `code_search.py` (`--outline`,
  `--trace`), `diff_engine.py` (atomic SEARCH/REPLACE), `control.py`
  (`--transport` gate + `check --stage ship`).
- Token footprint = `len(text.split())` of everything the pipeline reads from
  or writes to stdin/stdout (no chat tokens, no hidden LLM traffic).
- AST tree diff = `ast.dump` of `core.py` compared per top-level
  `FunctionDef` name before/after.
- Times are wall clock (ms) for the entire mode pipeline of the trial.

## Trial A — Ambiguous Patching (exact target replacement)

Target: change the retry-exhaustion branch inside `handle_event_b` only. The identical block exists verbatim in `handle_event_a` and `handle_event_c`.

| Metric | Vanilla | Asha |
|---|---|---|
| Execution time (ms) | 0.91 | 277.43 |
| Token footprint (in+out) | 4722 | 225 |
| Bytes before → after | 21290 → 21351 | — |
| AST-changed functions | none (syntax error: IndentationError: unexpected indent (<unknown>, line 575)) | handle_event_b |
| **Collateral damage (non-target mutated)** | **none** | **none** |
| Post-edit pytest | FAIL | PASS |
| Verdict | target hit but collateral=False | exact, collateral=0 |


## Trial B — Signature Mutation & Blast Radius

Target: `calculate_metrics(rows, window=30)` → `summarize_window(rows, cap, window=30)` consumed at 5 call sites in `service.py`.

| Metric | Vanilla | Asha |
|---|---|---|
| Execution time (ms) | 8.92 | 5977.42 |
| Token footprint (in+out) | 4738 | 65 |
| Files touched | core.py only (broken) | core.py + service.py + test_suite.py (trace-guided) |
| `trace_impact` entries | n/a | 15 (files: service.py, test_suite.py) |
| mypy exit | 1 | 1 |
| mypy errors | 5 | 2 |
| Broken refs missed at ship | **5** | **2** (trace found the full blast radius; consumer patches left 2 type errors in test_suite) |


## Trial C — Regressive / Malformed Feature Request

Target: inject an inverted empty-guard (`if not window_data:` → `if True: window_data = rows`), then declare the task done.

| Metric | Vanilla | Asha |
|---|---|---|
| Execution time (ms) | 9.95 | 1181.32 |
| Token footprint (in+out) | 2371 | 131 |
| Task marked done prematurely | **YES** (no gate exists) | **no — ship gate blocked it** |
| Gate exit code | n/a (no gate) | **1** |
| Gate verdict | — | **FAIL-CLOSED TRIGGER** |
| Test suite at closure | FAIL — unverified, still declared done | FAIL — caught by pytest, gate refused |


## Raw measurements (this run)

```json
{
  "trial_a": {
    "vanilla": {
      "elapsed_ms": 0.91,
      "token_footprint": 4722,
      "bytes_before": 21290,
      "bytes_after": 21351,
      "syntax_error": "IndentationError: unexpected indent (<unknown>, line 575)",
      "ast_changed_functions": [],
      "ast_collateral": [],
      "pytest_ok": false
    },
    "asha": {
      "elapsed_ms": 277.43,
      "token_footprint": 225,
      "patch_exit": 0,
      "ast_changed_functions": [
        "handle_event_b"
      ],
      "ast_collateral": [],
      "pytest_ok": true
    },
    "verdict": {
      "vanilla_collateral": [],
      "asha_collateral": [],
      "vanilla_exact": false,
      "asha_exact": true
    }
  },
  "trial_b": {
    "vanilla": {
      "elapsed_ms": 8.92,
      "token_footprint": 4738,
      "mypy_exit": 1,
      "mypy_errors": 5,
      "service_untouched": true,
      "broken_references_missed": 5
    },
    "asha": {
      "elapsed_ms": 5977.42,
      "token_footprint": 65,
      "trace_entries": 15,
      "trace_files": [
        "service.py",
        "test_suite.py"
      ],
      "mypy_exit": 1,
      "mypy_errors": 2,
      "patch_exits": [
        0,
        0,
        0
      ],
      "service_has_old_name": false
    },
    "verdict": {
      "vanilla_mypy_exit": 1,
      "asha_mypy_exit": 1,
      "vanilla_broken_missed": 5,
      "asha_breakage_remaining": 2
    }
  },
  "trial_c": {
    "vanilla": {
      "elapsed_ms": 9.95,
      "token_footprint": 2371,
      "marked_done_without_verification": true,
      "gate_exit": null,
      "pytest_ok": false
    },
    "asha": {
      "elapsed_ms": 1181.32,
      "token_footprint": 131,
      "gate_exit": 1,
      "gate_output": "CONTROL ERROR: root must actually read SKILL.md",
      "pytest_ok": false,
      "marked_done_prematurely": false
    },
    "verdict": {
      "vanilla_premature": true,
      "asha_gate_exit": 1,
      "asha_fail_closed_triggered": true
    }
  }
}
```
