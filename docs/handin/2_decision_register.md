# Hand-in 2 · Decision register

> What this shows (brief): *your answer, reason and evidence for each of the decisions in this brief.*
> Evidence is real output: transcripts from the deployed system (scenario run of 15 Nov 2026, 40/40 checks,
> [full report](../evidence/phase6_scenarios.md)), code output on the supplied snapshot, and the test suite.
> Simulated time is 4 Aug 2026; "now" is 10:00 unless stated.

**The rule behind every answer:** the LLM talks; deterministic code decides; the database is the last guard; a
coordinator confirms or takes over. Showing an option reserves nothing, a hold is not a booking, a request is not a
confirmation.

| # | Decision | Our answer in one line |
| --- | --- | --- |
| 1 | What to collect before showing options | Ask only: which load, arrival time/window, cause fixed?, latest finish, see vs book. Look up everything else. |
| 2 | Linking a conversation to driver, shipment, appointment | Driver from the verified token; one live load = link silently; two = ask; link written only when certain. |
| 3 | Revised ETA and its certainty | A window + confidence decided by code; vague = LOW and one question; plan on the late edge. |
| 4 | What makes a slot feasible | Eleven rules in one Python check, re-run inside every write. |
| 5 | "Available" while another driver decides | OPEN, no dock event, no booking claim, no unexpired hold. Looking reserves nothing. |
| 6 | Option -> hold -> request -> confirmed | Five states; only "requested" claims capacity; old slot kept until the coordinator confirms. |
| 7 | Simultaneous requests when capacity is short | First valid commit wins; in a surge, priority first. |
| 8 | When the facility schedule recalculates (optional) | On ETA change, gate-in, dock event, cancellation; CP-SAT vs dispatch rule; proposals only. |
| 9 | Stale options, cancellations, duplicates, retries | Slot versions; re-offer by priority; dedupe on id and text; idempotency keys. |
| 10 | No feasible slot | Code raises a complete hand-off; the agent invents nothing. |
| 11 | Saying no to a preferred slot | One plain reason, fresh options, no blame. |
| 12 | Human approval or takeover | A list of triggers, each enforced in code. |
| 13 | Proving no double booking | 100 rounds x 10 concurrent clients through the API, plus a DB-guard race; invariants clean. |

---

## UNDERSTAND

### 1 · What must you collect before you show useful options?

**Answer.** Ask the driver only for what the backend cannot know: **which load** (if more than one is live), the
**arrival time or window** at the gate, **whether the cause is fixed**, a **latest finish / leave-by** time if they
have one, and whether they want to **see or book**. Everything else (current appointment, priority, weight, dock
type, check-in, facility rules) is looked up. One question per message; after two failed clarifications a
coordinator takes over.

**Why.** Missing inputs produce wrong options; asking for backend facts wastes a driver's time; trusting the driver
over the backend creates wrong promises.

**Evidence: three chats and the fields asked** (deployed system).

| Chat | Driver said | Agent asked / did | Field collected |
| --- | --- | --- | --- |
| THR003, DRV013 | "I am late by one hour." | "Does that mean you'll reach the gate around 12:00, or could the delay keep growing?" | Arrival time (vague -> one question) |
| | "I'll reach the gate at 11:10, traffic cleared." | Showed 3 options, "not reserved yet" | Arrival 11:10 HIGH; cause fixed |
| THR010, DRV004 | "I will be late by 45 minutes." | "You have two live loads: ORD-260804-004 and ORD-260804-020. Which one will be late?" | Which load |
| | "The evening one, SHP1020" | "Does that mean you'll reach the gate around 18:45, or could the delay keep growing?" | Arrival time |
| THR011, DRV001 | "Just looking, not booking yet: I'll arrive between 12:30 and 13:00. What dock slots are there after 13:00?" | Options at 14:00, nothing held; compared them on request | See (not book); window 12:30-13:00 |

Nothing else was asked: weight, dock type, priority and current slot came from the database.

### 2 · How does a conversation link to the right driver, shipment and appointment?

**Answer.** The driver id comes from the **verified Cognito token** (`custom:driver_id`), never from the text or
the model. Code lists the driver's live loads: one -> link silently; two or more -> ask by order reference; a
cancelled load -> say so and point to dispatch. `chat_threads.shipment_id` is written only once the link is certain,
through the booking service, which refuses another driver's load (`NOT_YOURS`).

**Why.** One driver can hold two shipments (DRV004, DRV006); the model must never guess a join.

**Evidence: message-to-rows trace, THR010 (DRV004, two loads)**, local run on the snapshot:

| Driver message | Rows after it |
| --- | --- |
| "I will be late by 45 minutes." | `chat_threads` THR010-TRACE: driver DRV004, **shipment_id NULL**; `chat_messages`: DRIVER + AGENT; lookup: SHP1004 (WAITING), SHP1020 (ASSIGNED); **no ETA row** |
| "The evening one, SHP1020" | `chat_threads.shipment_id = SHP1020`; `audit_log` LINK_THREAD by driver:DRV004; **still no ETA row** (45 min is vague: asked first) |
| "Yes, I'll be at the gate around 18:45, the jam is clearing." | `eta_updates` + `eta_windows`: 18:45 HIGH; `driver_exceptions` OPEN, key DRV004-SHP1020-20260804; `APT1020` still CONFIRMED and current (untouched until a new booking is confirmed) |

Cancelled load: DRV008 asking "Is my 4 PM slot still active?" gets "the shipment was cancelled; please contact your
dispatcher" (`link_shipment` returns `CANCELLED`).

### 3 · How do you set the revised ETA and show how sure it is?

**Answer.** An ETA is a **window `[eta_lo, eta_hi]` + confidence**, decided by **code** from the extracted facts:

| Phrase type | Window | Confidence | Then |
| --- | --- | --- | --- |
| Stated time ("reaching 11:20") | that time | HIGH (MEDIUM if hedged) | Options |
| Stated range ("12:30-13:00") | the range | MEDIUM | Options planned on the **late edge** |
| Relative delay ("late by an hour") | plan + delay | **LOW** | One question for the arrival time |
| Repair time ("tyre fixed in 30 min") | - | **LOW** | "Once it's fixed, what time will you reach the gate?" |

Precedence: gate-in > ops override > warehouse estimate > latest driver update > shipment record > plan. Records
stamped after "now" are ignored. Feasibility always plans on `eta_hi + 15 min` (gate-to-dock). The model may not
record more certainty than code resolved; if extraction is unavailable (a provider error), only a message containing
an explicit clock time may be recorded above LOW. A LOW ETA cannot be held or requested.

**Why.** `v_latest_eta` ranks by recency only and stored "late by one hour" as an exact 11:00 (audit A12). A repair
time is not an arrival time.

**Evidence: the rule applied to real messages** (code output, message at 10:00):

| Message | Result |
| --- | --- |
| THR003 "I am late by one hour." | window 12:00-12:00, **LOW**, basis "plan 11:00 + 60 min (relative delay only)"; asks "Does that mean you'll reach the gate around 12:00, or could the delay keep growing?" |
| THR011 "between 12:30 and 13:00" | window 12:30-13:00, **MEDIUM**, "stated range; planning on the late edge" -> earliest dock 13:15 |
| THR001 "Reaching around 11:20" | 11:20, **HIGH** -> earliest dock 11:35 |
| "Tyre fixed in 30 min" | **LOW**, "repair estimate, not an arrival time"; asks "Once it's fixed, what time do you expect to reach the gate?" |

---

## DECIDE

### 4 · What makes a slot feasible for this shipment?

**Answer.** `check_feasibility(shipment, slots, now)` in Python; **all** must pass:
1. load is live and has no contradictory data (driver on two trucks); 2. slots belong to the destination facility;
3. every slot is OPEN; 4. no dock event (breakdown, maintenance, capacity reduction) overlaps them;
5. dock type: reefer -> reefer dock; > 25,000 kg -> heavy dock; standard -> standard dock (heavy dock only with
approval); 6. load <= dock max weight; 7. back-to-back slots cover the unload time; 8. start >= ETA late edge +
15 min and >= now; 9. no other booking occupies the dock during the unload (including one that runs past its own
slot); 10. no active booking claim or unexpired hold on any slot; 11. finish <= the driver's latest finish.
Start after 21:00 is allowed but needs approval. `docks.dock_status` is never read.

**Why.** Many rules apply at once, and an "AVAILABLE" row in `v_slot_availability` can be unusable (A1-A6, A16).

**Evidence: written check on three shipments** (code output on the snapshot, 10:00):

| Shipment | Facts | Options | Excluded (reason) |
| --- | --- | --- | --- |
| **SHP1010** HIGH, reefer, 16 t, 55 min, ETA 10:55 -> earliest 11:10 | | D5 12:00, D5 13:00, D5 14:00 | D1, D2, D3, D4, D6: "temperature-controlled load needs the reefer dock" |
| **SHP1014** CRITICAL, 21 t, 75 min, ETA 11:25 -> earliest 11:40 | | D2 12:00-13:15 (2 slots), D2 13:00, D4 13:00 | D1, D3: "max 20,000 kg < load 21,000 kg"; D5: reefer only |
| **SHP1016** HIGH, heavy, 31 t, 90 min, ETA 12:00 -> earliest 12:15 | | none | D1-D5: "31,000 kg needs the heavy dock"; D6: "Driver DRV007 is also on SHP1007 with VEH008; needs human review" |

Its existing pending booking APT1014A (D1 11:00) checked directly fails five rules at once: OVERWEIGHT;
SLOT_TOO_SHORT (75-min unload ends 12:15); BEFORE_ARRIVAL (earliest 11:40); DOCK_OCCUPIED (APT1017 occupies D1
12:00-13:00); SLOT_TAKEN. Tests: `tests/feasibility/test_feasibility.py` (15 cases).

### 5 · What does "available" mean while another driver is deciding?

**Answer.** Available = slot **OPEN**, **no overlapping dock event**, **no active booking claim** (including the
second slot of a multi-slot unload and a neighbour's overrun), and **no unexpired hold**. **Showing an option
reserves nothing.** Each quote stores the version of every slot in it; any hold, release, request or confirm bumps
the version, so at pick time the system knows whether the option changed (`STALE`, with fresh options).

**State it needs:** `appointment_slots.version`; `slot_quotes` (options + versions, 10-min life);
`slot_holds` + `slot_hold_claims` (one active per slot and per shipment, partial unique indexes, 5-min expiry);
`appointment_slot_claims` (one active claim per slot, partial unique index).

**Why.** Two readers can see one slot; only a write can claim it.

---

## COMMIT

### 6 · When does an option become a hold, a request, a reservation or a confirmed booking?

**Answer.**

| State | Who moves it | What changes | What the driver sees (real wording) |
| --- | --- | --- | --- |
| **Option** | Agent shows `find_slot_options` | Nothing reserved; quote + slot versions stored | "Here are some options, not reserved yet: 1. Dock D2, 12:00 - 13:00 ..." |
| **Held** | Driver picks a number they saw | 5-min hold, feasibility re-checked under row locks | "I have held Dock D2 for you from 12:00 to 13:00. This hold will expire in 5 minutes." |
| **Requested** | Driver says yes | PENDING appointment, `is_current = 0`, replaces the old one; claims block the slots; **old slot stays current** | "Requested, waiting for the warehouse. Keep your current slot until I confirm." |
| **Confirmed** | Coordinator presses Confirm (feasibility re-checked) | One transaction: old -> CANCELLED, new -> CONFIRMED + current | "Yes, your booking for Dock D2 from 12:00 to 13:00 is confirmed." |
| **Lapsed / rejected -> hand-off** | Sweeper (15 min after request or 30 min before start, never under 5 min) or coordinator | Slots freed, re-offered by priority, hand-off record | "Request rejected: <reason>" on the trip card; old slot kept |

Asked "So am I booked now?" while pending, the agent answered: "Your request for Dock D2 from 12:00 to 13:00 is
pending confirmation from the warehouse. You will keep your current slot until it is confirmed." A reply claiming
"booked" before CONFIRMED is blocked in code (0 such replies in 14 checked).

**Why.** Showing is not reserving; reserving is not confirming; keeping the old slot means a rejection never leaves a
driver with nothing. The 5-minute floor came from a live finding: a request for a slot starting now was otherwise
lapsed by the sweeper before any coordinator could see it.

### 7 · How do you order simultaneous requests when capacity runs short?

**Answer.** **Normal mode: first valid commit wins** (the database decides). **Surge mode** (>= 5 loads at a facility
with exception activity in the last 30 minutes): a slot shown to a **higher-priority** truck in the last 2 minutes is
kept for it while it decides; a lower-priority asker gets `PRIORITY_RESERVED` and fresh options. Order: priority
(CRITICAL > HIGH > NORMAL > LOW), then confirmed commitments are never displaced, then first come. Arriving early
never displaces booked work. (The spec's "batch for 2 minutes" made synchronous: same winner, no driver waits.)

**Why.** First-come is fair when capacity is loose; in a wave it lets a LOW load beat a CRITICAL one by typing faster.

**Evidence: policy tested on competing trucks** (deployed, scenario S3):

| Step | Result |
| --- | --- |
| Before | 4/5 loads active in 30 min: normal mode |
| DRV009 (SHP1009, CRITICAL) reports "reaching the gate at 10:50" | 5/5 -> **surge on** (SHP1006, 1009, 1012, 1014, 1016) |
| SHP1012 (NORMAL) tries D2 12:00, just shown to higher-priority loads | **PRIORITY_RESERVED** + fresh options |
| All four hold and request | SHP1014 CRITICAL **D2 12:00**; SHP1009 CRITICAL D1 13:00; SHP1006 HIGH D3 13:00; SHP1012 NORMAL D4 13:00 |
| Coordinator confirms all | 4/4 CONFIRMED; invariants clean; **0 priority violations** |

Tests: `tests/test_surge.py` (5 cases incl. expiry after 2 minutes and no surge = first come).

### 8 · (Optional) When does the facility schedule recalculate, which work is fixed, and what is the objective?

**Answer.** **Triggers:** an ETA change that breaks the current slot, a gate-in, a dock event, a cancellation or
no-show, an unload overrun. **Fixed:** trucks already unloading, dock events and blocked slots, and confirmed slots
starting in the next 60 minutes that the truck can still make. **Objective (plain words):** least priority-weighted
lateness against what we promised, then least waiting, then fewest changed promises; starts after 21:00 and standard
loads on the heavy dock only as a last resort; a truck with contradictory data never displaces confirmed work.
CP-SAT is compared with a dispatch rule (priority, then earliest arrival) on the same validator and score, starts
from the rule's plan so it is never worse, and **only proposes**; every change still goes hold -> request -> confirm.

**Evidence: rule, objective and replay** ([comparison](../evidence/phase5_comparison.md), scenario S7):
CP-SAT never worse than the rule and better at 6 of 9 snapshot times (09:00: weighted lateness -12%, waiting -21%,
proven optimal in 0.1 s); at the 10:00 wave they tie, so the simpler rule is recommended. Replay: at 10:00 the
current promises have 19 violations (SHP1005 on broken D3, SHP1014 on a too-weak D1, trucks booked before they can
arrive); after SHP1006 reports 12:40 the proposal moves it **D4 12:00 -> D3 13:00** (earliest 12:55); nothing is booked.

### 9 · How do you handle stale options, cancellations, duplicate messages and retries?

| Case | Mechanism | Replayed case (deployed system) |
| --- | --- | --- |
| **Stale option** | Slot versions in the quote; hold re-checks under row locks | S5: DRV004 is shown D2 10:00 (freed by cancelled APT1008); the coordinator gives it to SHP1005 by phone; DRV004 says "Option 1 please" -> "Sorry, that slot was just taken. Here are the updated options, not reserved yet: 1. Dock D2, 12:00-13:00 ..." |
| **Cancellation** | Freed slots re-offered to open exceptions they fit, highest priority first; event to the console | S5: coordinator rejects SHP1005's request -> freed SLOT-JAI-017/018 -> re-offer [SHP1004 (NORMAL), SHP1003 (LOW)] |
| **Duplicate message** | Redis `SET NX` on message id and on driver + text + 2-min bucket; unique index on channel message id | X1: THR001's text sent twice with one message id -> answered once (0.3 s, same reply); THR009-style retry with a new id also caught (test) |
| **Retry of a write** | Idempotency key on every write tool; advisory lock per key | Test: the same hold/request key twice -> one hold, one pending request, identical reply |

---

## FAIL SAFELY

### 10 · What happens when no feasible slot exists?

**Answer.** The tool that finds no option **raises the hand-off in code**; the agent says what was checked and that a
coordinator will contact them, and never invents a slot. The record holds: load, driver and phone, ETA window and
confidence, constraints, current slot, **every dock checked with its reason**, the nearest out-of-policy option,
data flags, routing and channel status. The console is the primary channel; email is marked NOT_SENT and a failed
email (OM004) is never treated as delivered.

**Evidence: escalation message and hand-off record** (THR005, SHP1015, 16:30).

Message (deployed): *"Your current appointment at D5 from 17:00-18:00 no longer fits. I checked for new slots after
18:30, but there are no feasible options for your reefer load at Jaipur DC. Dock D5 has planned refrigeration power
maintenance from 18:00-22:00. I have escalated this to a coordinator, and they will contact you shortly."*

Record (code output, abbreviated):

```json
{"reason_code": "NO_FEASIBLE_SLOT",
 "shipment": {"shipment_id": "SHP1015", "priority_code": "HIGH", "required_dock_type": "REEFER", "load_weight_kg": 15000},
 "driver": {"driver_id": "DRV015", "driver_name": "Mohammed Salim", "phone": "+91-9000010015"},
 "eta": {"eta_lo": "18:30", "eta_hi": "18:30", "confidence": "HIGH", "source": "DRIVER_DECLARED"},
 "earliest_dock_start": "18:45",
 "current_appointment": {"appointment_id": "APT1015A", "dock_code": "D5", "slot_start_ts": "17:00", "appointment_status": "CONFIRMED"},
 "feasible_options": [],
 "docks_checked": [{"dock_code": "D1", "detail": "D1: temperature-controlled load needs the reefer dock"},
                   {"dock_code": "D5", "detail": "D5: maintenance 18:00-22:00 (DEVT002: Planned refrigeration power maintenance)"}],
 "nearest_out_of_policy": {"none_today": true, "notes": ["D5 free after 22:00 (DEVT002); no later slots published"]},
 "routed_to": "SHIFT_MANAGER: Anita Joshi, +91-141-5001103",
 "channel_status": "console: QUEUED; email anita.joshi@setuhaul.example: NOT_SENT (console first); phone +91-141-5001103"}
```

### 11 · What do you say when the preferred slot is not granted?

**Answer.** One plain reason, the fresh options, no blame. Code returns the reason and the fresh quote with every
refusal (`SLOT_TAKEN`, `STALE`, `PRIORITY_RESERVED`, `NO_LONGER_FEASIBLE`), so the agent never has to invent one.

**Evidence: two refusals in plain words.**
1. Slot taken (deployed, S5): *"Sorry, that slot was just taken. Here are the updated options, not reserved yet:
   1. Dock D2, 12:00-13:00 2. Dock D1, 13:00-14:00 3. Dock D3, 13:00-14:00. Which option would you like?"*
2. Priority in a surge (booking service message, S3): *"In a busy wave this slot is being offered first to a
   higher-priority load. Here are other options."* (with fresh options attached)

Early arrival (prompt rule): arriving early does not move a truck ahead of trucks already booked; if a dock frees up
earlier, it is re-offered.

### 12 · Which decisions need human approval or takeover?

| Trigger | Approval or takeover | Where it is enforced |
| --- | --- | --- |
| Every revised booking | Coordinator **confirms** (no auto-confirm in V1) | `POST /bookings/{id}/confirm` is staff-only, own facility only |
| Start after 21:00 (RULE005) | Approval flag on the option | `feasibility.needs_approval` |
| Standard load on the heavy dock | Approval flag, ranked last | `dock_compatibility` |
| No feasible slot | **Takeover** | Raised by the options tool |
| Contradictory data (driver on two trucks, A8) | **Takeover**; booking blocked | `shipment_flags` -> feasibility |
| Driver safety, breakdown not fixed, penalties/customer commitments, disputes | **Takeover** | Prompt rule -> `escalate`; a promised hand-off is created in code if the model forgot |
| Two failed clarifications | **Takeover** | `TurnOutcome` middleware |
| Request rejected or lapsed | Hand-off record | `bookings.reject` / sweeper |
| Agent stopped by call limits, model/gateway outage | Fixed reply + hand-off | `TurnOutcome`, intake |

The agent never decides alone whether two drivers can share capacity, whether a vehicle fits a dock, how scarce
capacity is ranked, or whether a booking is committed: each is code or a person.

---

## PROVE

### 13 · How will you prove the system did not double-book capacity?

**Answer.** Attack it through the real HTTP API and then check the database with invariant queries.

**Evidence: concurrency test and result** (`tests/integrity/test_race.py`, [output](../evidence/phase6_test_output.txt)):

```
[threads]       100 rounds x 10 clients: 100 holds granted, 900 SLOT_TAKEN, 0 double holds; invariants clean
[processes]     100 rounds x 10 clients: 100 holds granted, 900 SLOT_TAKEN, 0 double holds; invariants clean
[request race]   20 rounds x 10 clients:  20 bookings, 180 ALREADY_REQUESTED; invariants clean
[db guard]      direct inserts bypassing app checks: exactly 1 claim per round; invariants clean
```

Invariants (also on the live database after the scenario run, `GET /admin/invariants`): at most one active claim per
slot; at most one active appointment per slot; no overlapping unloads on a dock for any booking the system made; at
most one current active appointment per shipment; no live hold on a booked slot; at most one active hold per slot;
claim rows agree with status. All empty. Deployed race (S4): SHP1006 and SHP1012 pick D2 12:00 at the same instant ->
one HTTP 200 HELD, one HTTP 409 SLOT_TAKEN with fresh options.
