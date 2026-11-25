# todo

A todo list for personal and work projects. One FastAPI process serves the React UI, the REST API, customer hubs, and the MCP server. Domain detail lives in the code comments.

## Checks

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
cd frontend && npm ci && npx tsc --noEmit
```

`requirements-dev.txt` includes `requirements.txt`. Pull requests run these checks in `.github/workflows/ci.yml`.

## Run locally

`TODO_DB=/tmp/todo.db .venv/bin/python main.py`, and in another terminal `cd frontend && npm run dev` (Vite proxies `/api`). Health is `/health`.

## Deploy

A push to `main` runs `.github/workflows/publish-container.yml` and publishes the container image. Portainer pulls that image.

Never touch the NAS, Portainer, or the live stack. Open pull requests only.

## Rules

- Never commit or echo secrets, tokens, hostnames, or internal IPs. This repository is public.
- Never commit to `main`.
