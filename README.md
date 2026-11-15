# SetuHaul FDE prototype

Driver delay conversations + dock re-slotting without infeasible or conflicting promises.
**The LLM talks, code decides.** Spec: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Skeleton, migrations, simulated clock, pinned versions | done |
| 2 | Booking service: feasibility, quotes, holds, requests, confirm/reject, escalate, sweeper, integrity test | next |
| 3 | LangChain agent, intake, middleware, Redis checkpointer, LangSmith, AgentCore entrypoint | |
| 4 | React driver chat + ops console, Cognito | |
| 5 | CP-SAT scheduler vs dispatch-rule baseline | |
| 6 | Scripted scenario run, brief steps 1-7 | |

## Layout

```text
booking/   FastAPI service, the ONLY SQLite writer (feasibility, holds, bookings, handoffs, sweeper, scheduling/)
agent/     LangChain v1 agent + intake workflow + AgentCore entrypoint (never opens SQLite)
web/       React + TypeScript (Vite): driver chat + ops console
tests/     integrity/ (race + invariants), feasibility/, scenarios/
evals/     LangSmith datasets and evaluators
infra/     deployment notes (ECS on EC2, AgentCore, Cognito, S3/CloudFront)
data/      supplied snapshot (read-only) + guide
docs/      spec and write-ups
```

## Run locally (Windows, no Docker needed)

```powershell
conda create -n setuhaul python=3.12 -y
conda activate setuhaul
pip install -r requirements-dev.txt -r agent/requirements.txt
copy .env.example .env      # then fill in keys (never commit .env)

python -m booking.db.migrate          # copies the snapshot to ~/.setuhaul/setuhaul.db and migrates the copy
uvicorn booking.api:app --port 8000   # http://localhost:8000/health and /docs
pytest                                # test suite
```

- The supplied `data/setuhaul_freight_operations.db` is opened read-only and never changed.
  `python -m booking.db.migrate --reset` throws the working copy away and starts again from the snapshot.
- The working DB lives outside OneDrive on purpose: sync clients lock files and break SQLite WAL.
- **Simulated clock:** defaults to `2026-08-04T09:30+05:30` and runs in real time from there.
  `POST /admin/clock` with `{"sim_ts": "...", "rate": 0}` to jump or freeze, or `{"advance_min": 5}`.

With Docker (optional): `docker compose up --build` runs the booking service; Redis is Upstash from `.env`.

## Pinned versions

Python **3.12** for both services.

| Package | Version | Used by |
| --- | --- | --- |
| fastapi | 0.163.1 | booking |
| starlette | 1.10.0 | booking |
| uvicorn[standard] | 0.54.0 | booking |
| pydantic | 2.14.3 | booking, agent |
| python-dotenv | 1.2.4 | booking, agent |
| redis (redis-py) | 8.1.0 | booking, agent |
| httpx | 0.28.1 | booking tests, agent tools |
| ortools (CP-SAT) | 9.15.6755 | booking/scheduling |
| langchain | 1.5.0 | agent |
| langchain-core | 1.8.1 | agent |
| langchain-openrouter | 0.2.9 | agent |
| langgraph | 1.2.22 | agent |
| langgraph-checkpoint | 4.3.2 | agent |
| langsmith | 0.14.17 | agent, evals |
| bedrock-agentcore | 1.26.0 | agent (Runtime entrypoint) |
| pytest | 9.2.0 | tests |
| Redis server | Upstash (managed, TLS) | cache, dedupe, checkpoints, pub/sub |
| Node / React / Vite | pinned in phase 4 | web |

Exact pins: `booking/requirements.txt`, `agent/requirements.txt`, `requirements-dev.txt`.

## Defaults for open assumptions (config, change on request)

15 min gate-to-dock · 5 min holds · pending requests expire at 15 min or 30 min before slot start
(whichever first) · standard loads may use the heavy dock only as a last-ranked option needing
coordinator approval · reefer/heavy rules applied by dock type at both facilities; 21:00 rule Jaipur only.
