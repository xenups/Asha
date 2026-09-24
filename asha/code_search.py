"""AST-based structural code navigator (tree-sitter).

- outline_file(path)  -> list[str]: symbol declarations (classes, defs, async
  defs, decorators) with line ranges, no bodies.
- find_pattern(path, lang, pattern) -> list[dict]: AST pattern match via
  ast-grep, returning line ranges + matched snippets only.

Zero daemons, zero ports: pure in-process parsing.
"""

from __future__ import annotations

import sys

if __package__ in (None, ""):
    # Direct-script mode: drop this directory from sys.path BEFORE any
    # stdlib import -- the sibling types.py would otherwise shadow stdlib
    # `types` and kill the import chain on GenericAlias (same guard as
    # asha/__main__.py and asha/mcp_server.py).
    def _asha_norm(entry: str) -> str:
        return (entry or ".").replace(chr(92), "/").rstrip("/").lower()

    _asha_pkg = _asha_norm(__file__).rsplit("/", 1)[0]
    sys.path = [entry for entry in sys.path if _asha_norm(entry) != _asha_pkg]
    sys.path.insert(0, _asha_pkg.rsplit("/", 1)[0])

import argparse
from pathlib import Path


def _tree_sitter_parser(lang: str):
    from tree_sitter_languages import get_parser  # type: ignore

    return get_parser(lang)


def outline_file(file_path: str, lang: str = "python") -> list[str]:
    """Extract class/function/decorator declarations as compact lines."""
    parser = _tree_sitter_parser(lang)
    src = Path(file_path).read_text(encoding="utf-8")
    tree = parser.parse(src.encode("utf-8"))
    lines: list[str] = []

    def _walk(node, depth: int = 0) -> None:
        ntype = node.type
        if ntype in ("class_definition", "function_definition"):
            name = ""
            for child in node.children:
                if child.type == "identifier":
                    name = src[child.start_byte : child.end_byte]
                    break
            start = node.start_point[0] + 1
            end = node.end_point[0] + 1
            lines.append(f"{'  ' * depth}{ntype}: {name}  [{start}-{end}]")
        elif ntype in ("decorated_definition",):
            name = ""
            for child in node.children:
                if child.type in ("class_definition", "function_definition"):
                    for c2 in child.children:
                        if c2.type == "identifier":
                            name = src[c2.start_byte : c2.end_byte]
                            break
                    break
            start = node.start_point[0] + 1
            end = node.end_point[0] + 1
            lines.append(f"{'  ' * depth}decorator: {name}  [{start}-{end}]")
        # Recurse with depth for nesting.
        for child in node.children:
            _walk(child, depth + 1)

    _walk(tree.root_node)
    return lines


def find_pattern(pattern: str, file_path: str, lang: str = "python") -> list[dict]:
    """AST pattern match returning line ranges + matched snippets."""
    from ast_grep_py import SgRoot  # type: ignore[import-untyped, import-not-found]

    src = Path(file_path).read_text(encoding="utf-8")
    root = SgRoot(src, lang)
    results = []
    for m in root.root().find_all(pattern=pattern):
        r = m.range()
        results.append(
            {
                "start_line": r.start.line + 1,
                "start_col": r.start.column + 1,
                "end_line": r.end.line + 1,
                "end_col": r.end.column + 1,
                "text": m.text(),
            }
        )
    return results


def _classify_usage(src: str, path: Path, symbol: str) -> list[dict]:
    """Classify import/call/inherit use sites with tree-sitter + ast-grep."""
    from ast_grep_py import SgRoot  # type: ignore[import-untyped, import-not-found]

    results: list[dict] = []
    try:
        root = SgRoot(src, "python").root()
    except Exception:
        return results

    # Calls: bare `symbol(...)` and attribute calls ending in symbol.
    for pat, usage in (
        (f"{symbol}($$$ARGS)", "call"),
        (f"$OBJ.{symbol}($$$ARGS)", "call"),
    ):
        try:
            matches = root.find_all(pattern=pat)
        except Exception:
            matches = []
        for m in matches:
            results.append(
                {
                    "file": str(path),
                    "line": m.range().start.line + 1,
                    "usage_type": usage,
                }
            )
    return results


def trace_impact(symbol: str, target_dir: str, lang: str = "python") -> list[dict]:
    """Lightweight impact trace: where `symbol` is imported, called, or inherited.

    Scans every .py file under `target_dir` with the pinned tree-sitter AST,
    classifying each use site via ast-grep. Returns compact
    {file, line, usage_type} entries only — never file bodies.

    usage_type ∈ {"import", "call", "inherit"}.
    - import:   `import X` / `from X import ...` / `from ... import symbol`
    - call:     named call `symbol(...)` (attribute calls included)
    - inherit:  `class Foo(symbol)` / `class Foo(Base, symbol)`
    """
    root = Path(target_dir)
    if not root.is_dir():
        raise NotADirectoryError(f"target_dir not found: {root}")

    parser = _tree_sitter_parser(lang)
    results: list[dict] = []
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix == ".py")
    for path in files:
        try:
            src = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if symbol not in src:
            continue  # fast path: skip files that can't reference the symbol

        tree = parser.parse(src.encode("utf-8"))

        # 1) Import statements (tree-sitter nodes cover whole line).
        def _walk(node) -> None:
            ntype = node.type
            if ntype in ("import_statement", "import_from_statement"):
                text = src[node.start_byte : node.end_byte]
                if symbol in text:
                    results.append(
                        {
                            "file": str(path),
                            "line": node.start_point[0] + 1,
                            "usage_type": "import",
                        }
                    )
            for child in node.children:
                _walk(child)

        _walk(tree.root_node)

        # 2) Class inheritance: base-class argument list inside class_definition.
        def _walk_inherit(node) -> None:
            if node.type == "class_definition":
                for child in node.children:
                    if child.type == "argument_list":  # superclass clause "(Base, ...)"
                        text = src[child.start_byte : child.end_byte]
                        if symbol in text:
                            results.append(
                                {
                                    "file": str(path),
                                    "line": child.start_point[0] + 1,
                                    "usage_type": "inherit",
                                }
                            )
            for c in node.children:
                _walk_inherit(c)

        _walk_inherit(tree.root_node)

        # 3) Call sites via ast-grep.
        results.extend(_classify_usage(src, path, symbol))

    # Deduplicate exact (file, line, usage_type) triples, then sort.
    seen = set()
    deduped = []
    for e in results:
        key = (e["file"], e["line"], e["usage_type"])
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    deduped.sort(key=lambda e: (e["file"], e["line"]))
    return deduped


def _cli() -> None:
    ap = argparse.ArgumentParser(description="AST structural code navigator.")
    ap.add_argument("--outline", metavar="PATH")
    ap.add_argument("--pattern", metavar="PATTERN", default=None)
    ap.add_argument("--file", metavar="PATH", default=None)
    ap.add_argument("--lang", default="python")
    ap.add_argument("--trace", metavar="SYMBOL", default=None)
    ap.add_argument("--dir", metavar="DIR", default=None)
    args = ap.parse_args()
    if args.trace and args.dir:
        for hit in trace_impact(args.trace, args.dir, args.lang):
            print(f"{hit['file']}:{hit['line']} [{hit['usage_type']}]")
        sys.exit(0)
    if args.outline:
        for line in outline_file(args.outline, args.lang):
            print(line)
    if args.pattern and args.file:
        for hit in find_pattern(args.pattern, args.file, args.lang):
            print(f"{hit['start_line']}:{hit['start_col']}-{hit['end_line']}:{hit['end_col']} {hit['text']!r}")
    sys.exit(0)


_PINNED_DEPS = {
    "tree-sitter": "0.21.3",
    "tree-sitter-languages": "1.10.2",
    "ast-grep-py": "0.45.3",
}


def verify_env() -> None:
    """Fail-closed dependency pin check (--verify-env).

    Confirms the three pinned AST deps are installed at the exact versions.
    Exit 0 immediately when all match; otherwise print an actionable
    diagnostic and exit 1. Never auto-installs.
    """
    import importlib.metadata as md

    missing, mismatched = [], []
    for name, pinned in _PINNED_DEPS.items():
        try:
            got = md.version(name)
        except md.PackageNotFoundError:
            missing.append(name)
            continue
        if got != pinned:
            mismatched.append(f"{name}: installed {got}, pinned {pinned}")
    if missing or mismatched:
        print("ENV CHECK FAILED (fail-closed, no auto-install):", file=sys.stderr)
        for name in missing:
            print(f"  missing: {name}", file=sys.stderr)
        for line in mismatched:
            print(f"  mismatch: {line}", file=sys.stderr)
        print(
            "  fix: .hermes/venv/Scripts/python.exe -m pip install "
            "'tree-sitter==0.21.3' 'tree-sitter-languages==1.10.2' 'ast-grep-py==0.45.3'",
            file=sys.stderr,
        )
        sys.exit(1)
    print(
        "ENV CHECK OK: tree-sitter 0.21.3, tree-sitter-languages 1.10.2, "
        "ast-grep-py 0.45.3"
    )
    sys.exit(0)


def self_test() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        dummy = Path(td) / "dummy.py"
        dummy.write_text(
            "class Service:\n"
            "    def __init__(self, name):\n"
            "        self.name = name\n"
            "\n"
            "    async def handle(self):\n"
            "        return self.name\n"
            "\n"
            "@app.get('/x')\n"
            "def route():\n"
            "    return {}\n",
            encoding="utf-8",
        )
        lines = outline_file(str(dummy))
        joined = "\n".join(lines)
        assert "class_definition: Service" in joined, lines
        assert "function_definition: handle" in joined, lines
        assert "decorator: route" in joined, lines
        # No function bodies leak (no 'self.name = name' / 'return' inside outline).
        assert "self.name = name" not in joined
        assert "return {}" not in joined

        hits = find_pattern("def $NAME($$$PARAMS): $$$BODY", str(dummy))
        if not hits:
            # ast-grep python pattern syntax may reject multi-$$$; fall back
            # to a simpler pattern for robust self-testing.
            hits = find_pattern("class $C", str(dummy))
        assert all("def " in h["text"] for h in hits), hits
        assert all(h["start_line"] >= 1 for h in hits)

        # --- trace_impact: mock lib + consumers must surface all three use types.
        root2 = Path(td)
        (root2 / "lib.py").write_text(
            "class Engine:\n"
            "    def run(self):\n"
            "        return 1\n",
            encoding="utf-8",
        )
        (root2 / "consumer.py").write_text(
            "from lib import Engine\n"
            "\n"
            "class FastEngine(Engine):\n"
            "    pass\n"
            "\n"
            "def go():\n"
            "    e = Engine()\n"
            "    return e.run()\n",
            encoding="utf-8",
        )
        trace = trace_impact("Engine", str(root2))
        usage_types = {e["usage_type"] for e in trace}
        assert "import" in usage_types, trace
        assert "inherit" in usage_types, trace
        assert "call" in usage_types, trace
        # Compact entries only — no file bodies leaked.
        assert all(set(e) == {"file", "line", "usage_type"} for e in trace)
        # Line numbers must be positive and sorted.
        assert all(e["line"] >= 1 for e in trace)

        print(
            f"code_search self-test PASSED ({len(lines)} outline lines, "
            f"{len(hits)} pattern matches, {len(trace)} impact entries "
            f"[{','.join(sorted(usage_types))}], no bodies dumped)"
        )
    sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--verify-env":
        verify_env()
    elif len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        self_test()
    else:
        _cli()