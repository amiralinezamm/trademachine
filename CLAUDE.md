# XAUUSD Trader Bot

Signal/trading bot for XAUUSD. Full spec lives in `SPEC.md` (added separately) — read it
before implementing any signal, feature, or backtest logic.

## Status

Base infrastructure only, set up 2026-09-12. Not yet implemented:
- MT5 connectivity (needs a Windows box we don't have yet)
- Real data backfill (no data ingested yet)
- n8n (deliberately not installed until asked for)
- Any actual signal/model/backtest logic

## Stack

- PostgreSQL 16 via Docker Compose, bound to `127.0.0.1:5432` only (see `docker-compose.yml`)
- FastAPI service in `src/api`, Python 3.11+ venv, run with uvicorn on port 8000 (localhost only for now)
- Docker Engine + Compose plugin (official docker.com apt repo)

## Layout

- `config/` — `params.yaml`, `event_map.yaml`, `costs.yaml` (strategy/event/cost config, placeholders until SPEC.md logic lands)
- `config/init-db/` — SQL run once against a fresh Postgres volume (schema bootstrap)
- `src/ingest`, `src/features`, `src/news`, `src/models`, `src/engine`, `src/backtest`, `src/discovery`, `src/api` — module boundaries per SPEC.md
- `n8n/` — reserved, not active yet
- `docs/`, `notebooks/`, `tests/` — as named

## Conventions

- Secrets live in `.env` (never committed) — see `.env.example` for required keys
- No hardcoded credentials in code or config committed to git
- Internal-only services (Postgres, future n8n/API) bind to `127.0.0.1` or the docker network, never `0.0.0.0` — this box holds financial data
