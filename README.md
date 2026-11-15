# SetuHaul FDE prototype

Driver delay conversations + dock re-slotting without infeasible or conflicting promises.
**The LLM talks, code decides.** Spec: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Skeleton, Postgres schema + snapshot loader, migrations, simulated clock, pinned versions | done (local); AWS provisioning blocked, see [infra/README.md](infra/README.md) |
| 2 | Booking service: feasibility, quotes, holds, requests, confirm/reject, escalate, sweeper, integrity test | next |
| 3 | LangChain agent, intake, middleware, Redis checkpointer, LangSmith, AgentCore entrypoint | |
| 4 | React driver chat + ops console, Cognito | |
| 5 | CP-SAT scheduler vs dispatch-rule baseline | |
| 6 | Scripted scenario run, brief steps 1-7 | |

## Changes from the spec (agreed with the project owner)

| Spec | Now | Why |
| --- | --- | --- |
| SQLite (WAL) file owned by the booking service | **Amazon RDS for PostgreSQL** | Managed system of record; no data on the app host |
| `BEGIN IMMEDIATE` single-writer lock | Transaction + `SELECT … FOR UPDATE` on the claimed slots (slot-id order), feasibility re-checked under the lock | Postgres equivalent; the same partial unique indexes are the last guard |
| DB file on the host, backed up to S3 | Supplied snapshot in a **private S3 bucket**, read into memory and loaded into RDS on first start | Nothing stored on EC2 |
| `slot_holds` only | `slot_holds` + `slot_hold_claims` (one row per slot) | A hold for a multi-slot unload (A6) must cover several slots while keeping one active hold per shipment |

The 18 supplied tables keep every column exactly as supplied (timestamps stay `TEXT`, `+05:30`);
the database uses the `C` collation so text timestamps sort exactly in time order. The only
added column is `appointment_slots.version`.

## Layout

```text
booking/   FastAPI service, the ONLY writer of capacity (feasibility, holds, bookings, handoffs, sweeper, scheduling/)
  db/      Postgres access, snapshot loader (S3 or local), migrations
agent/     LangChain v1 agent + intake workflow + AgentCore entrypoint (never touches the DB)
web/       React + TypeScript (Vite): driver chat + ops console
tests/     integrity/ (race + invariants), feasibility/, scenarios/
evals/     LangSmith datasets and evaluators
infra/     AWS deployment notes and scripts
data/      supplied data package (seed SQL is the loader's input)
docs/      spec
```

## Run locally (Windows, no Docker needed)

```powershell
conda create -n setuhaul python=3.12 -y
conda activate setuhaul
conda install -c conda-forge postgresql=16 -y
pip install -r requirements-dev.txt -r agent/requirements.txt
copy .env.example .env      # fill in keys (never commit .env)

# one-time local Postgres
initdb -D $HOME\.setuhaul\pgdata -U postgres -A trust -E UTF8 --locale=C
pg_ctl -D $HOME\.setuhaul\pgdata -l $HOME\.setuhaul\pg.log start

python -m booking.db.migrate          # creates DB 'setuhaul', loads the snapshot once, applies migrations
uvicorn booking.api:app --port 8000   # http://localhost:8000/health and /docs
pytest                                # clones a fresh test DB per test
```

- `python -m booking.db.migrate --reset` drops the database and reloads from the snapshot.
- **Simulated clock:** defaults to `2026-08-04T09:30+05:30`, then runs in real time.
  `POST /admin/clock` with `{"sim_ts": "...", "rate": 0}` to jump or freeze, or `{"advance_min": 5}`.

With Docker (optional): `docker compose up --build` runs Postgres + the booking service.

## Pinned versions

Python **3.12**, PostgreSQL **16**.

| Package | Version | Used by |
| --- | --- | --- |
| fastapi | 0.163.1 | booking |
| starlette | 1.10.0 | booking |
| uvicorn[standard] | 0.54.0 | booking |
| pydantic | 2.14.3 | booking, agent |
| psycopg[binary] | 3.3.9 | booking |
| psycopg-pool | 3.3.3 | booking |
| boto3 | 1.44.3 | booking (S3 snapshot) |
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
