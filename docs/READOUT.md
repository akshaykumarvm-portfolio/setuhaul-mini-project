# SetuHaul prototype: readout

**Problem.** Delayed drivers need a revised dock slot they can rely on, while 20-35 requests in a 30-minute wave hit 6
docks with breakdowns, reefer/heavy rules and vague ETAs. The hard part is not the chat: it is **never telling a
driver something that isn't true** ("you're booked", a slot inside a breakdown, a dock that can't take the load) while
capacity is contested.

**Answer.** The LLM talks; deterministic code decides; the database is the last guard; a coordinator confirms or takes
over. Showing an option reserves nothing, a hold is not a booking, a request is not a confirmation, and every write
re-checks feasibility under row locks.

## What runs (AWS, account 688508115450, us-east-1)

| Layer | Built with |
| --- | --- |
| Driver chat + coordinator/planner console | React 18 + Vite, S3 + CloudFront, Cognito sign-in (driver / coordinator / planner) |
| Conversation agent | LangChain v1 `create_agent` + guardrail middleware, OpenRouter model, Upstash Redis checkpoints, on Bedrock AgentCore Runtime (JWT inbound auth) |
| Booking service (sole writer of capacity) | FastAPI on ECS/EC2, RDS PostgreSQL (private), snapshot loaded from private S3 |
| Facility scheduler | OR-Tools CP-SAT vs dispatch-rule baseline, proposals only |
| Observability | LangSmith: one trace per driver message, rubric feedback, regression dataset |
| Cost | $20/month budget alert; `infra/aws/stop.sh` / `start.sh` |

## Evidence

| Brief success measure | Target | Result | Where |
| --- | --- | --- | --- |
| Conflicting or duplicate allocations | 0 | **0**: 100 rounds x 10 concurrent clients (threads and processes) through the HTTP API, plus a race that bypasses app checks; invariant queries clean; also 0 on the live DB after the scenario run | `phase2/6 evidence`, `tests/integrity/` |
| Options found infeasible at confirmation | 0 committed | **0**: feasibility re-checked inside hold, request and confirm; overlapping-unload invariant clean | `tests/test_booking_flow.py`, invariants |
| No-slot cases escalated with a complete hand-off | 100% | **100%**: THR005 raised by code with every dock checked and why, nearest out-of-policy option, routing, channel status | S6 |
| Driver clarification turns | <= 2 | **1** max | S1, S2 |
| Turns to a usable outcome | < 3 | **2** max (vague delay -> clarify -> options) | S1 |
| Priority-policy violations | 0 | **0**: in a surge a NORMAL load cannot take a slot just shown to a CRITICAL one | S3, `tests/test_surge.py` |
| Replies claiming booked/confirmed before CONFIRMED | 0 | **0** of 14 live replies, two consecutive runs | S1-S7, LangSmith feedback |
| Scripted scenario checks (brief steps 1-7 + duplicates) | all | **40/40, twice** | [phase6_scenarios.md](evidence/phase6_scenarios.md) |
| Automated tests | all | **93 pass** | [phase6_test_output.txt](evidence/phase6_test_output.txt) |
| CP-SAT vs dispatch rule | justify CP | never worse; better at 6 of 9 snapshot times; tie at the 10:00 wave, so the rule is kept there | [phase5_comparison.md](evidence/phase5_comparison.md) |

## What we learned building it (each fixed and tested)

* **The spec's own example would have promised a busy dock**: a 75-minute unload in a 60-minute slot runs into the next
  slot; bookings now occupy their real unload time.
* **A prompt is not a guarantee.** The live model said "I'll pass this to the coordinator" without doing it, held an
  option the driver hadn't seen, and asked "would you like to see options?" instead of showing them. Each is now
  enforced in code: hand-offs raised by the tool, holds only on options already shown, options rendered from the quote
  when the reply omits them, "booked" blocked unless the booking service says CONFIRMED.
* **A provider outage must cost a question, not a promise.** An OpenRouter 429 on extraction let "late by 45 minutes"
  through as a usable ETA; extraction now retries and falls back, and without a code-resolved window an ETA can only be
  trusted if the driver stated a clock time.
* **A request for a slot starting now lapsed on arrival** (its 30-min-before-start deadline was already past); every
  request now gets at least 5 minutes for a coordinator.
* **Records stamped after "now" must not count** on a simulated clock (a 16:25 ETA was visible at 10:00).
* **CP-SAT needed policy, not just maths**: priority-weight every truck's lateness, make approvals a last resort, keep
  contradictory-data trucks from displacing confirmed work, and start from the baseline so it is never worse.

## Deliberate deviations from the spec

RDS PostgreSQL instead of SQLite (row locks + the same partial unique indexes); a service key in SSM instead of a
Cognito machine-to-machine token (cost); the browser reaches AgentCore through the booking service with the driver's
own token (validated twice); surge priority applied synchronously (same winner as a 2-minute batch, no waiting);
range ETAs are MEDIUM on their late edge; custom Redis checkpointer (Upstash lacks RediSearch). All listed in the
README with reasons.

Hand-ins 1-3 (problem sheet + data audit, decision register, prototype): [docs/handin/](handin/README.md).

## Not done / next

* Gate-in and dock-in events are read from the snapshot; there is no gate-scanner endpoint yet.
* Notifications to drivers and coordinators beyond the console (SMS/WhatsApp, email delivery tracking).
* A real "now": the prototype runs on the 4 Aug 2026 snapshot with a simulated clock.
* Pilot plan: one facility (Jaipur), two weeks alongside phone re-planning; go/no-go on 0 wrong promises and fewer
  coordinator calls per exception.
