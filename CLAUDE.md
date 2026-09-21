# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.
See [README.md](README.md) for full documentation.

## Setup

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Always run inside the venv.

## Configuration

Configuration lives in `config.py`, but **every value is overridable by an
environment variable of the same name**, and `config.py` loads `.env` at import.

- Set `MODEL_NAME` and `LM_STUDIO_BASE_URL` in `.env`, not in `config.py`.
- `MODEL_NAME` must match an id the endpoint actually serves.
- **Never commit credentials.** `config.py` is tracked; `.env` is git-ignored.

## Running

```bash
python main.py                  # interactive agent CLI
python server.py                # HTTP API on 127.0.0.1:8000
python run_scheduler.py         # scheduled glucose reports
cd web && bun dev               # front-end on :3001 (needs server.py running)
```

## Benchmark and analysis

```bash
python -m benchmark --model <model>     # 72-task OhioT1DM suite
python -m analysis.analyze              # per-model charts + summary
python -m analysis.compare              # cross-model comparison
python -m analysis.paper_figures        # regenerate assets/ and tables/
```

The benchmark requires the OhioT1DM dataset in `data/`. It is **not** in this
repository and must not be committed — it is under a Data Use Agreement that
prohibits redistribution.

## Tests

```bash
python -m pytest                # 118 tests, no network needed
```

Tests mock the LLM endpoint, LibreLinkUp, and the scheduler. The
benchmark-isolation tests need the OhioT1DM dataset present.

## Layout

`agent/` core agent and tools · `benchmark/` harness · `analysis/` scoring and
figures · `scripts/` sweep scripts · `web/` front-end · `docs/` design notes.
