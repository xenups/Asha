# Blind review record

Six patches exported from the benchmark worktrees with the condition stripped,
renamed to neutral ids (shuffled with seed 7), and handed to a separate evaluator
together with the frozen acceptance criteria. The evaluator applied and ran each
patch in a throwaway copy of the target component and scored nine dimensions.
The mapping was withheld until the scores were returned.

Mapping (revealed after scoring):

| id | task | condition |
|---|---|---|
| W3 | T1 | baseline |
| E2 | T1 | asha |
| C5 | T2 | baseline |
| N1 | T2 | asha |
| B9 | T3 | baseline |
| F7 | T3 | asha |

## Applied and tested by the reviewer (real output)

| id | apply | pytest tail | exit |
|---|---|---|---|
| BASE | — | `37 passed in 0.45s` | 0 |
| W3 | OK | `39 passed in 0.47s` | 0 |
| E2 | OK | `39 passed in 0.47s` | 0 |
| C5 | OK | `38 passed in 0.56s` | 0 |
| N1 | OK | `38 passed in 0.61s` | 0 |
| B9 | OK | `55 passed in 0.61s` | 0 |
| F7 | OK | `60 passed in 0.57s` | 0 |

## Scores (0-3 per dimension)

| id | funct | regress | maintain | arch | err-handling | scope | tests | types/static | docs | total |
|---|---|---|---|---|---|---|---|---|---|---|
| W3 (T1 baseline) | 2 | 1 | 2 | 3 | 2 | 3 | 2 | 3 | 2 | **20** |
| E2 (T1 asha) | 3 | 2 | 3 | 3 | 2 | 3 | 2 | 3 | 3 | **25** |
| C5 (T2 baseline) | 1 | 2 | 3 | 3 | 2 | 2 | 1 | 3 | 1 | **18** |
| N1 (T2 asha) | 3 | 2 | 2 | 3 | 3 | 2 | 2 | 2 | 3 | **22** |
| B9 (T3 baseline) | 2 | 3 | 2 | 2 | 2 | 3 | 3 | 3 | 2 | **22** |
| F7 (T3 asha) | 3 | 3 | 3 | 3 | 2 | 3 | 3 | 3 | 3 | **26** |

The reviewer's per-task verdicts were E2 > W3, N1 > C5, F7 > B9 — i.e. the Asha
implementation won all three pairs, on code quality alone, without knowing the
condition.

## Findings that matter for the release decision

* **C5 (baseline, T2) fails its own acceptance in the real configuration.** With
  `OutputManager(PRETTY)` built at import, the warning still reached stdout
  (138 bytes measured); in JSON/TXT mode `logger.remove()` left no sinks, so the
  warning disappeared entirely. Its new test patches the logger, so it cannot
  catch the defect it claims to fix. This is a *baseline* failure the blind
  reviewer found on its own.
* **N1 (asha, T2) fixes the layer where the defect lives** and was verified
  byte-level in all three output modes, but its own regression test is
  order-dependent: `pytest tests/test_s3_resolver.py` alone fails because loguru
  binds the sink to the `sys.stderr` object present at `logger.add` time. It also
  ships a `black --check` violation and a hardcoded level number `30`.
* **W3 (baseline, T1) silently loses URLs** that the base commit at least emitted:
  markdown links, parenthesised and quoted URLs return nothing because `\S+`
  tokenises the leading punctuation and only the right side is trimmed.
* **F7 (asha, T3) is the cleanest refactor**: no scheme strings left in either
  consumer, detection handles prose/wrapped/multiline forms; residual issues are
  duplicate hits from its two regex passes and inherited trailing punctuation.
* **B9 (baseline, T3) only half-satisfies its own AC1** — `_S3_URL_PATTERN` and the
  `"/s3/" or ".s3."` sniff still live in the resolver, while its helper docstring
  claims to be the single place that knows S3 URL syntax.

## Reviewer's own declaration of limits

* mypy was not available in the reviewer's environment, so static typing was scored
  by reading, not by running the type checker.
* T2 and T3 verdicts rest on probes the reviewer wrote, not on the patches' own
  tests — which is exactly how C5's self-fulfilling test was caught.
* No fuzzing beyond hand-built corpora.
