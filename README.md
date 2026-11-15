# SetuHaul FDE prototype

Driver delay conversations + dock re-slotting without infeasible or conflicting promises.
**The LLM talks, code decides.** Spec: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Skeleton, Postgres schema + snapshot loader, migrations, simulated clock, pinned versions | done (local); AWS provisioning pending, see [infra/README.md](infra/README.md) |
| 2 | Booking service: feasibility, quotes, holds, requests, confirm/reject, escalate, sweeper, integrity test | done (local); test output in [docs/evidence/phase2_test_output.txt](docs/evidence/phase2_test_output.txt) |
| 3 | LangChain agent, intake, middleware, Redis checkpointer, LangSmith, AgentCore entrypoint | next |
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
| Slot is free if no appointment row sits on it | A booking occupies its dock for its **real unload time**, even past its slot | APT1009 (75 min in a 60-min slot on D4 11:00) unloads until 12:15, so D4 12:00 is *not* free. The spec's own example ("D2 or D4 at 12:00" for SHP1006) would have promised a busy dock |
| ETA = latest update | Records stamped after the simulated "now" are ignored; a driver update recorded by the system wins a tie | The snapshot holds 09:35+ updates; without this a new driver ETA lost to a "future" seed row |
| Default "now" 09:30 | **10:00** | After every morning ETA report in the data, so SHP1006's 11:20 update exists |
| Holds need only feasibility | Holds and requests also refuse a **LOW-confidence ETA** (`ETA_UNCONFIRMED`) and **contradictory driver data** (A8) | Code, not only the agent prompt, stops a promise built on a vague ETA or a driver on two trucks |
| 10 concurrent `request_booking` for one slot (Decision 13) | Race tested at three layers: 10 clients hold one slot; 10 clients request one hold; 10 writers bypass app checks and insert directly | Holds are exclusive per slot, so 10 requests for one slot can only arrive through one hold, or through a bug |
| Surge-mode batching (Decision 7) | Not in phase 2: first valid commit wins | Built with the phase 6 competing-trucks scenario |

The 18 supplied tables keep every column exactly as supplied (timestamps stay `TEXT`, `+05:30`);
the database uses the `C` collation so text timestamps sort exactly in time order. The only
added column is `appointment_slots.version`.

## Booking API (phase 2)

Writes accept an `Idempotency-Key` header. Refusals: HTTP 409 (404 for unknown ids) with `{"ok": false, "code": ...}`;
lock wait over 5 s: 503 `BUSY`.

| Method + path | Purpose | Refusal codes |
| --- | --- | --- |
| `GET /drivers/{id}/context` | Live shipments, current appointment, ETA window, check-in, flags | `UNKNOWN_DRIVER` |
| `POST /eta` | Record an ETA window + confidence; re-checks the current slot and any hold | `SHIPMENT_NOT_ACTIVE`, `BAD_WINDOW` |
| `POST /quotes` | Up to 3 ranked options (not reserved) + why each other dock was excluded | `UNKNOWN_SHIPMENT` |
| `POST /holds` | Pick an option: 5-min hold, re-checked under row locks | `SLOT_TAKEN`, `STALE`, `NO_LONGER_FEASIBLE`, `ETA_UNCONFIRMED` (with fresh options) |
| `DELETE /holds/{id}` | Release a hold | |
| `POST /bookings` | Hold -> pending request (the step that claims capacity) | `HOLD_EXPIRED`, `ALREADY_REQUESTED`, `SLOT_TAKEN`, `NO_LONGER_FEASIBLE` |
| `POST /bookings/{id}/confirm` | Coordinator confirms: old -> CANCELLED, new -> CONFIRMED + current, one transaction | `INVALID_STATE`, `PENDING_EXPIRED`, `NO_LONGER_FEASIBLE` |
| `POST /bookings/{id}/reject` | Coordinator rejects: slots freed, hand-off created | `INVALID_STATE` |
| `GET /bookings?status=` | Pending queue for the console, with expiry times | |
| `GET /shipments/{id}/status` | NONE / HELD / PENDING / CONFIRMED in plain fields | |
| `POST /handoffs`, `GET /handoffs`, `POST /handoffs/{id}/close` | Escalation with the full record (docks checked, nearest out-of-policy option, routing) | |
| `GET /facilities/{id}/timeline` | Dock timeline: every slot, booking, hold and dock event | |
| `POST /admin/clock`, `POST /admin/sweep` | Simulated clock; run the sweeper now (it also runs every 30 s) | |

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
- **Simulated clock:** defaults to `2026-08-04T10:00+05:30` (after every morning ETA report in the data), then runs in real time.
  Records stamped after the simulated time are treated as not yet happened.
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
