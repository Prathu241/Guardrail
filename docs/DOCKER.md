# Running Guardrail with Docker

Step-by-step, from a fresh machine to the dashboard in your browser. For a public HTTPS server
with API keys, see [GUIDE.md section 9](GUIDE.md#9-deploying-to-production).

## 1. Install Docker

| Your computer | Install |
|---|---|
| Windows 10/11 | Docker Desktop (it turns on WSL 2 during setup; restart when asked) |
| macOS (Intel or Apple silicon) | Docker Desktop |
| Linux | Docker Engine and the Compose plugin (`docker-compose-plugin` package) |

Start Docker Desktop, then check in a terminal (PowerShell on Windows):

```bash
docker --version
docker compose version      # needs v2.24 or newer
```

## 2. Get the code

```bash
git clone https://github.com/Prathu241/Guardrail.git
cd Guardrail
git checkout claude/agent-action-guardrails-scefpj   # the branch with the current code
```

## 3. Start it

```bash
docker compose up --build
```

The first build downloads Python and the dependencies (a few minutes). It is ready when the log
shows `Uvicorn running on http://0.0.0.0:8000`. Open **http://localhost:8000** for the dashboard,
and http://localhost:8000/docs for the API.

To run it in the background instead: `docker compose up -d --build`.

## 4. Try it

In a second terminal, in the same folder:

```bash
curl http://localhost:8000/healthz                       # "status":"ok", "worker":"running"
docker compose exec guardrail guardrail demo             # the three-act demo
docker compose exec guardrail guardrail ask "My order O-5001 arrived with a cracked lantern. Can I get a refund for the lantern?" --customer C-1001
docker compose exec guardrail guardrail approvals list
```

Windows PowerShell: use `curl.exe` instead of `curl`. In the dashboard, the Console tab has
sample messages to send; escalated actions appear in the Approvals tab.

## 5. Use the real Claude models (optional)

```bash
cp .env.example .env          # Windows: copy .env.example .env
```

Edit `.env`: set `ANTHROPIC_API_KEY=...`, `GUARDRAIL_REVIEWER=claude` and, for Claude as the
agent too, `GUARDRAIL_AGENT=claude`. Then restart with `docker compose up -d --build`. `.env`
is in `.gitignore`; never commit it.

## 6. Everyday commands

| To | Run |
|---|---|
| See the logs | `docker compose logs -f guardrail` |
| See status and health | `docker compose ps` |
| Stop (keeps the data) | `docker compose down` |
| Start again | `docker compose up -d` |
| Rebuild after `git pull` | `docker compose up -d --build` |
| Reset to fresh demo data | `docker compose exec guardrail guardrail init-db --reset` |
| Delete everything, data included | `docker compose down -v` |
| Back up the data | `docker compose exec guardrail guardrail backup /data/backups` |
| Run the end-to-end container test (Linux, macOS, WSL) | `make docker-smoke` |

The data (database, approvals, audit log) lives in the Docker volume `guardrail-data`, so it
survives restarts and rebuilds.

## 7. If something goes wrong

| Symptom | Fix |
|---|---|
| `Cannot connect to the Docker daemon` | Start Docker Desktop (or `sudo systemctl start docker` on Linux) and wait until it says it is running |
| `port is already allocated` / port 8000 in use | In `docker-compose.yml` change `"8000:8000"` to `"8080:8000"` and open http://localhost:8080 |
| `env_file ... required` or `path` not allowed | Your Compose is older than v2.24: update Docker Desktop, or create an empty `.env` file and change the `env_file` entry to `- .env` |
| Build fails with `CERTIFICATE_VERIFY_FAILED` (company network or proxy) | Build with your proxy's CA certificate: `docker build --secret id=ca,src=path/to/ca.pem -t guardrail .`, then `docker compose up` |
| Container keeps restarting | `docker compose logs guardrail`. In production mode it refuses unsafe settings and prints exactly what to fix |
| Health shows `"status":"degraded"` or `worker_errors` | Read the error in `/healthz`; `docker compose restart guardrail` |
| `scripts/docker_smoke.sh: bad interpreter` on Windows | Run it from WSL, or re-clone with `git config --global core.autocrlf false` |

## 8. Going to production

Use the production setup instead of `docker-compose.yml`: HTTPS via Caddy, API keys with roles,
production mode, a read-only container. Short version:

```bash
cp deploy/.env.production.example deploy/.env.production    # fill in DOMAIN, API keys, ANTHROPIC_API_KEY
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.production up -d --build
```

The full steps, checks and runbook are in [GUIDE.md](GUIDE.md#9-deploying-to-production).
