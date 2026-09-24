# Development requirements

- Run Python and development tools through `uv run --locked`.
- Strict type checking is required for all Python source and tests. Keep the mypy settings in `pyproject.toml` enabled.
- Do not introduce `Any`, type-check suppression comments, file exclusions, or relaxed type-check settings without explicit user permission.
- Validate external data at runtime before using it as a concrete type; use `object` at untyped library boundaries rather than propagating dynamic types.
- Before completing changes, run:
  - `uv run --locked mypy --platform win32`
  - `uv run --locked mypy --platform linux`
  - `uv run --locked -m unittest discover -v`
