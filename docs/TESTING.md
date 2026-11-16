# End-to-end test guide (user's view)

What you are checking above all: **nobody is ever told they have a slot that isn't real.** Options are "not
reserved", a hold is "not booked", a request is "waiting for the warehouse", and only a coordinator's confirm makes it
"confirmed". Two drivers can never end up with the same dock time.

## 0. Before you start

| | |
| --- | --- |
| App | `https://<your CloudFront domain>` (printed by `infra/aws/60_web.sh`, also in `var/aws/web.env`) |
| Password (all demo users) | `aws ssm get-parameter --name /setuhaul/DEMO_PASSWORD --with-decryption --query Parameter.Value --output text` (Git Bash: prefix `MSYS_NO_PATHCONV=1`) |
| Drivers | `drv006` `drv013` `drv004` `drv014` `drv012` `drv001` `drv015` `drv008` |
| Coordinators | `neha.jaipur` (Jaipur DC), `rohit.gurugram` (Gurugram DC) |
| Planner | `planner` (both facilities, simulated clock, reset) |
| If AWS is paused | `bash infra/aws/start.sh` (2-5 min), afterwards `bash infra/aws/stop.sh` |

Use **two browser windows** (one normal, one private/incognito) so a driver and a coordinator can be signed in at the
same time. Phone-sized window for the driver (or open it on your phone), desktop for the console.

1. Sign in as `planner` → **Reset demo data** (reloads the 4 Aug 2026 snapshot, clock frozen at 10:00).
2. Sign out. Keep the console window for `neha.jaipur`.

The simulated clock is frozen unless you move it, so holds and request deadlines only lapse when the planner
advances time (+5 / +15 min) or presses **Run sweeper**.

## 1. Vague delay → clarify → options → hold → request → confirm (THR003, the core flow)

Driver window: sign in as **drv013**.

| Step | You type / do | Expect |
| --- | --- | --- |
| 1 | `I am late by one hour.` | A question: will you reach the gate around 12:00, or could it grow? (vague ETA = LOW confidence, no options yet) |
| 2 | `I'll reach the gate at 11:10, traffic cleared` | ETA recorded; up to 3 options, numbered, **"not reserved yet"**; trip card ETA shows 11:10 HIGH |
| 3 | `Option 1` | "Held … for 5 minutes", asks whether to request. Trip card: purple **hold** banner with countdown, stepper on *Held* |
| 4 | `Yes, request it` | "Requested, waiting for the warehouse. Keep your current slot until I confirm." Stepper on *Requested* |
| 5 | `Am I booked now?` | Says it is **pending**, never "booked/confirmed" |

Console window: sign in as **neha.jaipur**. The *Live* badge is green. The request appears under **Awaiting your
confirmation** with the move (old slot struck through → new slot), priority, weight and a lapse countdown; the slot is
striped amber on the timeline.

| 6 | Click **Confirm** | Card disappears; the slot turns blue (confirmed) on the timeline |
| 7 | Back in the driver window (within ~5 s) | Green banner **"Warehouse confirmed: D2 at 12:00."**; stepper on *Confirmed* |

## 2. Two loads, unclear which (THR010)

Driver **drv004**: `I'll be late by about 45 minutes` → the assistant asks **which load** (two shown as chips) →
tap/say `the evening load SHP1020` → it asks whether you'll reach the gate around 18:45 (the delay was vague) rather
than guessing.

## 3. Two drivers, one slot (race, Decision 13)

Window A **drv006**, window B **drv012** (another private window).
1. A: `Traffic after Shahpura, reaching the gate at 11:20` → if asked which load, `SHP1006` → get options.
2. B: `Tyre repaired, reaching the gate at 11:10` → get options. Note an option both lists share (e.g. D2 12:00).
3. A picks it (`Option 1`) → held. B picks the same slot → B is told it **was just taken** and gets fresh options.
The console shows one hold (purple), never two.

## 4. Coordinator says no

Continue any driver to a request, then in the console click **Reject** and give a reason. The driver window shows
**"Request rejected: <reason>"**, keeps the old slot, and a **REQUEST REJECTED** hand-off appears in the console.

## 5. Hold or request lapses

Get a driver to a hold (step 3 above) and stop. Planner: **+5 min** then **Run sweeper** → the hold is gone from the
timeline; asking the assistant to request now gets "the hold lapsed, pick again". A request left pending lapses at
15 min (or 30 min before the slot), becomes a **PENDING LAPSED** hand-off, and the driver keeps the old slot.

## 6. No feasible slot → hand-off with a full record (THR005)

Planner: clock **16:30**. Driver **drv015**: `Evening traffic, reaching the gate at 18:30. Need a slot for my reefer load.`
→ the assistant says nothing fits and that a coordinator will contact you (it never invents a slot). Console:
**NO FEASIBLE SLOT** hand-off; expand it to see the driver's phone, ETA, every dock checked with its reason
(D5: maintenance 18:00-22:00, DEVT002), the nearest out-of-policy option, routing (shift manager) and channel status
(console first; email marked NOT_SENT, never treated as delivered). **Mark resolved** closes it. Planner: clock back to **10:00**.

## 6b. Facility re-plan (CP-SAT vs dispatch rule)

Sign in as **planner** (or a coordinator). Set the clock to **09:00**, then in **Facility re-plan** press **Propose
schedule**. Expect three columns (current promises / dispatch rule / CP-SAT) on the same metrics, the recommended plan
highlighted (CP-SAT at 09:00, with lower weighted lateness and waiting), a Gantt of that plan (outlined bars = promise
changed, hatched = breakdown/maintenance/unloading), and a per-truck table with differences shaded. Expand **Why the
current promises can't all hold** to see the impossible promises (21 t load on a 20 t dock, slot inside a breakdown...).
At **10:00** the two methods tie and the dispatch rule is recommended. Nothing on the timeline changes: it is a
proposal only. A driver can ask the assistant "what's the scheduler suggesting for me?" and gets only their own line,
marked not reserved.

## 7. Edge cases worth one message each

| Driver | Message | Expect |
| --- | --- | --- |
| drv008 | `Is my 4 PM slot still active?` | The load was cancelled; contact dispatch (THR012) |
| drv014 | `Origin loading finished late, reaching the gate at 11:25` | 21 t CRITICAL load: only D2/D4 (two back-to-back slots for a 75-min unload); D1/D3 are too weak (A1/A2) |
| drv006 | `link SHP1014` / `book SHP1014` | Refused: that load isn't assigned to you |
| drv001 | `Arriving between 12:30 and 13:00, just want to see options` | Options planned on 13:00 (+15 min gate-to-dock), nothing held (ask-only, THR011) |
| any | Send the same message twice quickly | Second copy answered with the first reply, not processed again |

## 8. Security checks (optional, terminal)

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://<domain>/api/me                 # 401: no token
curl -s -o /dev/null -w "%{http_code}\n" https://<domain>/api/bookings \
  -H "Authorization: Bearer <a driver's ID token>"                                 # 403: drivers can't see the queue
```
`rohit.gurugram` cannot see or act on Jaipur's queue; only `planner` can move the clock or reset.

## 9. Behind the scenes

* **LangSmith** project `setuhaul`: one `driver_message` trace per message (extraction, every middleware step, model
  calls, tool calls), tagged with thread and shipment.
* **CloudWatch**: `/setuhaul/booking` (booking service), AgentCore runtime logs under `/aws/bedrock-agentcore/`.
* **Audit**: every hold, request, confirm, reject and hand-off is in `audit_log` with who did it (`coordinator:neha.jaipur`).

## Running the same flows locally (no AWS)

```powershell
uvicorn booking.api:app --port 8000          # AUTH_MODE=off by default
python -m agent.app serve                     # agent on :8080
cd web; npm install; npm run dev              # http://localhost:5173 (persona picker instead of sign-in)
```
