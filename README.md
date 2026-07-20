# HLAF — Hermes Line Agent Framework

HLAF is a Python backend framework skeleton designed to eventually power a LINE
AI Agent that integrates with Hermes Desktop. **This repository currently
contains only the project scaffold** — no LINE integration, no Hermes
integration, and no AI/agent logic has been implemented yet.

## Tech Stack

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) — package & environment manager
- [FastAPI](https://fastapi.tiangolo.com/) — web framework
- [Pydantic v2](https://docs.pydantic.dev/latest/) / `pydantic-settings` — data validation & settings
- [Ruff](https://docs.astral.sh/ruff/) — linting & formatting
- [Pytest](https://docs.pytest.org/) — testing
- [python-dotenv](https://github.com/theskumar/python-dotenv) — `.env` loading
- [httpx](https://www.python-httpx.org/) — async HTTP client
- [loguru](https://github.com/Delgan/loguru) — logging

## Project Structure

```
HLAF/
├── app/
│   ├── api/            # FastAPI routers (HTTP entrypoints)
│   ├── bridge/         # Integration bridge layer (reserved)
│   ├── hermes/         # Hermes Desktop integration (reserved)
│   ├── router/         # Internal request/message routing (reserved)
│   ├── prompts/        # Prompt templates (reserved)
│   ├── tools/          # Agent tools (reserved)
│   ├── rag/            # Retrieval-augmented generation (reserved)
│   ├── memory/         # Agent/conversation memory (reserved)
│   ├── models/         # Pydantic models / schemas
│   ├── services/       # Business logic / service layer
│   ├── utils/          # Shared utilities
│   ├── config.py       # Settings (pydantic-settings)
│   ├── logger.py       # Loguru logger setup
│   └── main.py         # FastAPI application entrypoint
├── config/             # Non-Python runtime configuration files
├── docs/                # Project documentation
├── scripts/             # Developer helper scripts (Windows PowerShell)
├── tests/                # Pytest test suite
├── .env.example          # Environment variable template
├── Makefile               # Common dev commands (Unix/macOS/WSL)
├── pyproject.toml          # Project metadata, dependencies, tool config
└── LICENSE                  # MIT License
```

## Installation

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.12+.

```bash
# Clone the repository
git clone https://github.com/unique487/hlaf-hermes-line-agent-framework.git
cd hlaf-hermes-line-agent-framework

# Install dependencies (creates a .venv automatically)
uv sync

# Copy environment template
cp .env.example .env    # Windows: copy .env.example .env
```

## Run

```bash
# Development (with auto-reload)
uv run uvicorn app.main:app --reload

# Production-like
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Once running, visit:

- `GET /` — hello world
- `GET /health` — health check → `{"status": "ok"}`
- `GET /docs` — Swagger UI

## Development

Common commands are provided via `Makefile` (Linux/macOS/WSL) and
`scripts/dev.ps1` (Windows PowerShell).

| Task                 | Makefile      | PowerShell                  |
|----------------------|---------------|------------------------------|
| Install dependencies | `make install`| `./scripts/dev.ps1 install`  |
| Run (reload)         | `make dev`    | `./scripts/dev.ps1 dev`      |
| Run (no reload)      | `make run`    | `./scripts/dev.ps1 run`      |
| Run tests            | `make test`   | `./scripts/dev.ps1 test`     |
| Lint                 | `make lint`   | `./scripts/dev.ps1 lint`     |
| Format               | `make format` | `./scripts/dev.ps1 format`   |
| Lint + test          | `make check`  | `./scripts/dev.ps1 check`    |
| Clean caches          | `make clean`  | `./scripts/dev.ps1 clean`    |

Add a new dependency:

```bash
uv add <package>
uv add --dev <package>   # dev-only dependency
```

## Testing

```bash
uv run pytest
```

Tests live under `tests/` and use FastAPI's `TestClient` (backed by `httpx`).

## Lint

```bash
uv run ruff check .
```

## Formatting

```bash
uv run ruff format .
```

## License

[MIT](LICENSE)
