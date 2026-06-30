# Repository Guidelines

## Project Structure & Module Organization

RAGuardian is a Python 3.11+ Flask RAG service. Core application code lives in `app/`: `app/app.py` wires UI/API routes, `app/utils/` holds retrieval, provider, auth, workspace, indexing, and ingestion logic, `app/templates/` contains Jinja views, and `app/static/` contains browser assets. Tests are in `tests/` and follow the same feature boundaries as the app. Operational docs and API specs are in `docs/`. The static marketing site is in `public-site/`. External clients live under `integrations/`, currently the WordPress plugin in `integrations/wordpress/rag-client/`.

## Build, Test, and Development Commands

- `python3 -m venv .venv` and `source .venv/bin/activate`: create and enter the local virtual environment.
- `python -m pip install -r requirements.txt`: install runtime dependencies.
- `cp .env.example .env`: create a local configuration file.
- `python app/app.py`: run the local Flask app at `http://127.0.0.1:5000/`.
- `gunicorn -c gunicorn.conf.py wsgi:application`: run the production WSGI entry point.
- `python -m pip install -r requirements-dev.txt` then `.venv/bin/pytest -q`: install and run the Python test suite.
- `cd integrations/wordpress/rag-client && npm test`: run WordPress plugin linting and PHPUnit tests.

## Coding Style & Naming Conventions

Use 4-space indentation for Python and follow the existing PEP 8 style. Prefer `snake_case` for functions, variables, modules, and test names; use `PascalCase` for classes and uppercase for constants. Keep route orchestration in `app/app.py` thin when possible, with reusable behavior in `app/utils/`. No project-wide formatter is configured, so match nearby code and avoid unrelated reformatting.

## Testing Guidelines

Pytest is configured in `pyproject.toml` to discover `tests/`. Name Python tests `test_*.py` and test functions `test_<behavior>`. Use `tmp_path`, in-memory state, and local fixtures rather than real `app/data`, uploads, Chroma indexes, or secrets. Add regression coverage for auth, workspace isolation, provider configuration, API behavior, and ingestion changes. For WordPress UI changes, use the plugin E2E scripts after `npm run doctor`.

## Commit & Pull Request Guidelines

Recent history uses short imperative summaries, sometimes with conventional prefixes such as `feat:`, `fix`, and `feat(wp):`. Prefer `type(scope): concise summary` when a scope is clear. Pull requests should describe the behavior change, list test commands run, call out config or migration impacts, link related issues, and include screenshots for UI or `public-site/` changes.

## Security & Configuration Tips

Never commit `.env`, workspace data, uploads, ChromaDB files, logs, API keys, or connector secrets. Start from `.env.example`, keep integration API keys server-side, and grant the narrowest scopes needed (`query`, `ingest`, `speech`).
