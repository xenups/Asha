# Self-monitoring module (medium level default)

- Pulse after every tool execution batch (`control.py pulse --event tool`).
- Record failures verbatim; never summarize away error output.
- Refuse work while open questions exist or evidence digests mismatch.
- Keep the working tree clean before any `check --stage ship`.
