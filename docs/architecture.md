# Architecture

This document describes the high-level structure of the HLAF (Hermes Line Agent Framework) codebase.

## Directory Layout

- `app/` — application source code
  - `api/` — FastAPI routers (HTTP entrypoints)
  - `bridge/` — integration bridge layer (reserved for future Hermes Desktop bridge)
  - `hermes/` — Hermes Desktop integration (reserved, not yet implemented)
  - `router/` — internal message/request routing logic
  - `prompts/` — prompt templates (reserved for future AI features)
  - `tools/` — agent tool implementations (reserved for future AI features)
  - `rag/` — retrieval-augmented generation components (reserved)
  - `memory/` — conversation/agent memory components (reserved)
  - `models/` — Pydantic models / data schemas
  - `services/` — business logic / service layer
  - `utils/` — shared utility functions
  - `config.py` — application settings (pydantic-settings)
  - `logger.py` — centralized loguru logger setup
  - `main.py` — FastAPI application entrypoint
- `config/` — non-Python configuration files (e.g. YAML/JSON) used at runtime
- `tests/` — automated tests (pytest)
- `scripts/` — developer helper scripts
- `docs/` — project documentation

## Status

This is currently a project skeleton only. No LINE, Hermes, or AI/agent logic has been implemented yet.
