# Hand-in 1 · Problem Definition Sheet and data audit

SetuHaul Logistics · dock re-slotting for delayed drivers · data snapshot 4 August 2026 (Asia/Kolkata)

> What this shows (brief): *facts, estimates, assumptions and unknowns are kept apart; every data issue reported
> comes with the query that proves it.* All queries below were run on the supplied
> `setuhaul_freight_operations.db`; their real output is in the appendix.

## 1. The problem

**Problem statement.** When a truck runs late, the driver needs a revised dock appointment they can rely on, fast,
while many delayed trucks compete for a few compatible docks. Today promises are made from a stale picture
(a dock marked ACTIVE while broken, a vague "late by an hour" stored as an exact time, nothing that claims a slot
while someone decides), so two drivers can be promised one slot, or a driver can be promised a slot that cannot work.

**How might we** let drivers report delays, ask questions and consider revised appointments in a conversation,
while many simultaneous requests meet limited dock capacity, **without infeasible or conflicting promises**?

**The hard part** is not the chat. It is never telling a driver something that isn't true ("you're booked", a slot
inside a breakdown, a dock that can't take the load) while capacity is contested and ETAs are uncertain.

| Who | Need | Measured on |
| --- | --- | --- |
| Driver (primary; on a phone, mid-route) | A slot that will actually be there, in few messages | Turns and minutes to a usable answer; waiting at the gate |
| Warehouse coordinator | Confirm or take over with a complete picture, not phone tag | Calls per exception; dock utilisation; on-time priority loads |
| Warehouse planner / head of operations | Policy that holds under a wave of delays; evidence it works | Double bookings (0); priority violations (0); detention |

**In scope:** one driver's exception conversation; slot feasibility; holds, requests and confirmations; escalation
to a coordinator; facility-level re-sequencing (optional tool). **Out of scope (brief):** network optimisation,
carrier selection and rate talks, autonomous driver-safety decisions, customs/hazmat/legal, commercial penalties.

## 2. Success measures (from the brief's rubric)

| Measure | Target | How it is measured |
| --- | --- | --- |
| Conflicting or duplicate allocations | 0 | Concurrency test (100 rounds x 10 clients) + invariant queries on the live DB |
| Options later found infeasible | 0 committed | Feasibility re-checked inside hold, request and confirm |
| No-slot escalations handled correctly | 100% complete hand-off records | Field check on every hand-off |
| Driver clarification turns | <= 2 per resolved case | Questions asked per case in the scenario run |
| Time from first message to usable outcome | < 3 turns | Turns and seconds per scenario |
| Priority-policy violations | 0 | Each allocation in a surge checked against the stated policy |
| Share resolved without manual takeover | report, don't target | Count over the scenario set (escalating is correct behaviour) |
| Driver waiting after rescheduling; slot utilisation | report | Scheduler metrics (waiting after earliest start; dock minutes used / open) |

## 3. Facts, estimates, assumptions, unknowns

| Type | Statement | Source / proof |
| --- | --- | --- |
| **Fact** | 2 facilities, 9 docks (Jaipur D1-D6, Gurugram D1-D3), 21 shipments, 106 slots, 18 tables | Row counts; database guide |
| **Fact** | The DB enforces one active appointment per slot and one current active appointment per shipment, nothing else about capacity | `ux_active_appointment_per_slot`, `ux_current_active_appointment_per_shipment` |
| **Fact** | Jaipur: reefer loads must use D5; loads over 25,000 kg must use the heavy dock (D6); no new start after 21:00 without approval | RULE003, RULE004, RULE005 |
| **Fact** | Dock limits: D1/D3 20 t, D2/D4 25 t, D5 reefer 22 t, D6 heavy 35 t; standard slots 60 min, D6 slots 90 min | `docks`, `appointment_slots` |
| **Fact** | No live GPS: an ETA is plan, driver declaration, warehouse estimate or ops override | Brief p.3; `eta_updates.source_type` |
| **Fact** | A dock event (breakdown, maintenance, capacity reduction) is not reflected in `docks.dock_status` or slot status | A3, A4, A5 |
| **Estimate** | Gate-in to dock-in is about 15 minutes (SHP1001 20 min, SHP1002 7 min) | `facility_checkins` (2 samples) |
| **Estimate** | Driver ETAs are optimistic: 9 of 12 updates are self-declared HIGH (1 MEDIUM, 2 LOW), and a vague "late by one hour" is still stored as an exact 11:00 | `eta_updates.confidence_code`; A12 |
| **Estimate** | DRV006 cannot unload in Jaipur after 11:20 and reach Gurugram by 14:00 (~240 km) | A8 (road distance approximate) |
| **Assumption** | A coordinator confirms every revised booking (no auto-confirm in V1) | Brief: "a person approves or takes over"; confirm with ops |
| **Assumption** | A hold lasts 5 minutes; a pending request lapses 15 min after the request or 30 min before the slot, whichever is first, but never sooner than 5 min after it was made | Decision 6; the 5-minute floor came from a live finding |
| **Assumption** | A standard load may use the heavy dock only as a last-ranked option with coordinator approval | Not stated in the data; confirm with the planner |
| **Assumption** | Reefer and heavy rules apply by dock type at both facilities; the 21:00 rule only at Jaipur | Rules exist only for Jaipur; Gurugram has no heavy dock |
| **Assumption** | "Now" for the prototype is 10:00 on 4 Aug 2026; records stamped later have not happened yet | A15, A17 |
| **Unknown** | What "now" is in the snapshot: records run 07:10-16:30 while morning threads are still open | A15 |
| **Unknown** | Whether a truck inside the check-in grace period counts as late (SHP1004 LATE at 25 min, grace 30) | `facility_checkins` vs RULE002 |
| **Unknown** | Who owns night-shift escalations at Gurugram (role vacant, no email) | A13 |
| **Unknown** | Whether an unload longer than its slot may take the next slot; the snapshot already has four such bookings | A6, A16 |
| **Unknown** | Where the revised ETAs on four shipments came from (no update record, no confidence) | A18 |

## 4. Data audit (19 issues, each proved by a query)

| # | Issue | Evidence (query result) | Risk if ignored | Design response (implemented) |
| --- | --- | --- | --- | --- |
| A1 | Confirmed booking breaks the dock weight limit | APT1005: SHP1005 20,500 kg on D3 (max 20,000) | Truck arrives at a dock that can't take it | Feasibility checks weight; existing violations flagged to the coordinator by the scheduler |
| A2 | A chat-made pending booking is also overweight | APT1014A: SHP1014 21,000 kg on D1 (max 20,000), source DRIVER_CHAT | Proves a chat flow already promised an infeasible slot | Every option, hold, request and confirm re-checks feasibility |
| A3 | An OPEN slot sits inside a breakdown | SLOT-JAI-030 (D3 09:00-10:00) OPEN; DEVT001 starts 09:15 | "Available" slot that cannot be used | Available = OPEN **and** no overlapping dock event |
| A4 | A capacity reduction is not reflected in slots | DEVT003 on D2 08:00-09:20; SLOT-JAI-015/016 OPEN | Over-promising D2 in the morning | Dock events block their window |
| A5 | `docks.dock_status` is stale | D3 and D5 ACTIVE despite BREAKDOWN / MAINTENANCE | Trusting the status column | `dock_status` is never read; state comes from events at time t |
| A6 | Unload time longer than the slot | SHP1002 70 min, SHP1005/1009/1014 75 min in 60-min slots | Next truck finds the dock busy | Bookings claim consecutive slots; claim table + unique index |
| A7 | Live shipments with no current appointment | SHP1012, SHP1016 (HIGH), SHP1018, SHP1021 | "No appointment" treated as an error | A normal state the agent resolves (options, not an error) |
| A8 | One driver on two in-transit trucks; one truck on a tight back-to-back | DRV007: SHP1007 (VEH008) and SHP1016 (VEH007) both IN_TRANSIT; DRV006/VEH015: Jaipur 11:20 then Gurugram 14:00 | Booking a truck that can't physically come | Contradictory data blocks booking -> human review; scheduler gives it the lowest weight |
| A10 | Duplicate exception from a messaging retry | EXC001 and EXC009 share key DRV006-SHP1006-20260804-0934; no unique constraint | Double-processing, double holds | Redis dedupe on message id and on driver+text+2 min; unique index on live dedupe keys and on channel message id |
| A11 | No-show not reconciled with the shipment | APT1018 NO_SHOW but SHP1018 still ASSIGNED; same truck VEH003 is waiting in the yard on SHP1003 | Wrong "late" story; wasted slot | Explained by the truck's earlier load; flagged, not auto-cancelled |
| A12 | Low-confidence or ranged ETA stored as an exact time | SHP1013 "late by one hour" stored as 11:00; SHP1017 "12:30-13:00" stored as 12:45; both copied to `latest_eta_ts` | A promise built on a guess | ETA = window + confidence; vague -> one question; plan on the late edge |
| A13 | Escalation channel broken | OM004 to night.shift@... FAILED (not a known contact); CON005 has no email | Escalation silently lost | Console first; failed or missing email recorded, never treated as delivered |
| A14 | Confirmed/in-progress appointments already infeasible | SHP1005 on broken D3; SHP1006 ETA 11:20 vs slot ending 11:00; SHP1015 ETA 18:30 vs D5 17:00-18:00 (maintenance from 18:00) | Drivers drive to promises that cannot hold | Every ETA update re-checks the current appointment |
| A15 | The snapshot has no single "now" | Records span 07:10-16:30 while 09:xx threads are still open | Time-dependent rules give different answers | A simulated clock injected into every rule |
| A16 | **Existing bookings physically overlap** | APT1014A (75 min, D1 11:00) runs into APT1017 (D1 12:00, confirmed); APT1002 (70 min) into APT1004; APT1009 (D4 11:00) leaves D4 12:00 "free" while still unloading | A free-looking slot that is occupied | A booking occupies its dock for its real unload time, not just its slot |
| A17 | A later update leaks into the shipment row | SHP1015 `latest_eta_ts` 18:30 = ETA010, which is stamped 16:25 | At 10:00 the system "knows" a 16:25 ETA | Rows stamped after now are ignored, incl. a `latest_eta_ts` equal to a future update |
| A18 | Revised ETAs with no record behind them | SHP1002, SHP1009, SHP1010, SHP1011 have `latest_eta_ts` <> original but no `eta_updates` row | Unknown source and confidence | Used at MEDIUM confidence, labelled SHIPMENT_RECORD |
| A19 | Pending requests flagged as current and already stale | APT1013A (booked 09:32) and APT1014A (booked 09:52) PENDING with `is_current = 1` | A request treated as a commitment; never lapses | Requests are created not-current; the old slot stays until confirm; stale requests lapse to a hand-off |

**What the two database rules do not cover:** booking a BLOCKED slot; a dock of the wrong type or too weak for the
load; a slot in another facility; unloads that run past their slot into the next one; a slot inside a dock event; a
second claim on the *second* slot of a multi-slot unload; holds that never expire; duplicate exceptions; a pending
request treated as current. The prototype covers each in code and, where possible, with an extra database guard
(claim tables with partial unique indexes, a trigger refusing bookings on non-OPEN slots, unique dedupe indexes).

**What the snapshot cannot tell us:** real gate-to-dock times (two samples), how ETAs drift during a trip (no GPS),
which customer commitments or penalties attach to a load, who staffs the Gurugram night shift, and how many
requests arrive in a real wave (the brief's figures are used to size the design).

## 5. A ladder of situations (seeded cases, simple to hard)

| Rung | Case | Seed | What it tests |
| --- | --- | --- | --- |
| 1 | Late ETA reported, slot can be moved | SHP1006 / THR001 | Feasibility + re-slot |
| 2 | Vague delay | SHP1013 / THR003 | Clarification before options |
| 3 | Two live loads, unclear which | DRV004 / THR010 | Linking the conversation |
| 4 | Ask-only, compare options | SHP1017 / THR011 | Nothing reserved by looking |
| 5 | Several trucks chase the same window | SHP1006, 1009, 1012, 1014 | Concurrency + priority |
| 6 | Option disappears / cancellation frees capacity | APT1008 | Stale options, re-offer |
| 7 | No feasible slot | SHP1015 / THR005 | Escalation with a complete hand-off |
| 8 | Contradictory data | DRV007 | Human review before booking |

## Appendix · audit queries and their output

Run on the supplied SQLite snapshot (read-only). A2 is in A1's result, A4 in A3's, A9 in A8's.

### A1

```sql
SELECT a.appointment_id, a.shipment_id, a.appointment_status, d.dock_code, d.max_vehicle_weight_kg, s.load_weight_kg
FROM appointments a JOIN appointment_slots sl USING (slot_id) JOIN docks d ON d.dock_id = sl.dock_id
JOIN shipments s USING (shipment_id)
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') AND s.load_weight_kg > d.max_vehicle_weight_kg;
```

| appointment_id | shipment_id | appointment_status | dock_code | max_vehicle_weight_kg | load_weight_kg |
| --- | --- | --- | --- | --- | --- |
| APT1014A | SHP1014 | PENDING_CONFIRMATION | D1 | 20000 | 21000 |
| APT1005 | SHP1005 | CONFIRMED | D3 | 20000 | 20500 |

2 row(s)

### A3

```sql
SELECT e.dock_event_id, e.event_type, sl.slot_id, sl.slot_start_ts, sl.slot_end_ts, sl.slot_status
FROM dock_status_events e JOIN appointment_slots sl ON sl.dock_id = e.dock_id
WHERE sl.slot_start_ts < e.event_end_ts AND sl.slot_end_ts > e.event_start_ts AND sl.slot_status = 'OPEN';
```

| dock_event_id | event_type | slot_id | slot_start_ts | slot_end_ts | slot_status |
| --- | --- | --- | --- | --- | --- |
| DEVT001 | BREAKDOWN | SLOT-JAI-030 | 2026-08-04T09:00:00+05:30 | 2026-08-04T10:00:00+05:30 | OPEN |
| DEVT003 | CAPACITY_REDUCTION | SLOT-JAI-015 | 2026-08-04T08:00:00+05:30 | 2026-08-04T09:00:00+05:30 | OPEN |
| DEVT003 | CAPACITY_REDUCTION | SLOT-JAI-016 | 2026-08-04T09:00:00+05:30 | 2026-08-04T10:00:00+05:30 | OPEN |

3 row(s)

### A5

```sql
SELECT d.dock_code, d.dock_status, e.event_type, e.event_start_ts, e.event_end_ts
FROM docks d JOIN dock_status_events e USING (dock_id);
```

| dock_code | dock_status | event_type | event_start_ts | event_end_ts |
| --- | --- | --- | --- | --- |
| D3 | ACTIVE | BREAKDOWN | 2026-08-04T09:15:00+05:30 | 2026-08-04T13:00:00+05:30 |
| D5 | ACTIVE | MAINTENANCE | 2026-08-04T18:00:00+05:30 | 2026-08-04T22:00:00+05:30 |
| D2 | ACTIVE | CAPACITY_REDUCTION | 2026-08-04T08:00:00+05:30 | 2026-08-04T09:20:00+05:30 |

3 row(s)

### A6

```sql
SELECT a.appointment_id, a.shipment_id, s.expected_unload_min,
  (strftime('%s', substr(sl.slot_end_ts,1,19)) - strftime('%s', substr(sl.slot_start_ts,1,19)))/60 AS slot_min
FROM appointments a JOIN appointment_slots sl USING (slot_id) JOIN shipments s USING (shipment_id)
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
  AND s.expected_unload_min > (strftime('%s', substr(sl.slot_end_ts,1,19)) - strftime('%s', substr(sl.slot_start_ts,1,19)))/60;
```

| appointment_id | shipment_id | expected_unload_min | slot_min |
| --- | --- | --- | --- |
| APT1014A | SHP1014 | 75 | 60 |
| APT1002 | SHP1002 | 70 | 60 |
| APT1005 | SHP1005 | 75 | 60 |
| APT1009 | SHP1009 | 75 | 60 |

4 row(s)

### A7

```sql
SELECT s.shipment_id, s.current_status, s.priority_code FROM shipments s
WHERE s.current_status NOT IN ('COMPLETED','CANCELLED')
  AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.shipment_id = s.shipment_id AND a.is_current = 1
                  AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS'));
```

| shipment_id | current_status | priority_code |
| --- | --- | --- |
| SHP1012 | IN_TRANSIT | NORMAL |
| SHP1016 | IN_TRANSIT | HIGH |
| SHP1018 | ASSIGNED | LOW |
| SHP1021 | ASSIGNED | NORMAL |

4 row(s)

### A8

```sql
SELECT driver_id, vehicle_id, shipment_id, current_status, latest_eta_ts, destination_facility_id
FROM shipments WHERE current_status NOT IN ('COMPLETED','CANCELLED')
  AND (driver_id IN (SELECT driver_id FROM shipments WHERE current_status NOT IN ('COMPLETED','CANCELLED')
                     GROUP BY driver_id HAVING COUNT(*) > 1)
    OR vehicle_id IN (SELECT vehicle_id FROM shipments WHERE current_status NOT IN ('COMPLETED','CANCELLED')
                      GROUP BY vehicle_id HAVING COUNT(*) > 1))
ORDER BY driver_id, latest_eta_ts;
```

| driver_id | vehicle_id | shipment_id | current_status | latest_eta_ts | destination_facility_id |
| --- | --- | --- | --- | --- | --- |
| DRV003 | VEH003 | SHP1003 | WAITING | 2026-08-04T08:20:00+05:30 | FAC-JAI-01 |
| DRV003 | VEH003 | SHP1018 | ASSIGNED | 2026-08-04T11:50:00+05:30 | FAC-JAI-01 |
| DRV004 | VEH004 | SHP1004 | WAITING | 2026-08-04T09:20:00+05:30 | FAC-JAI-01 |
| DRV004 | VEH004 | SHP1020 | ASSIGNED | 2026-08-04T18:00:00+05:30 | FAC-JAI-01 |
| DRV006 | VEH015 | SHP1006 | IN_TRANSIT | 2026-08-04T11:20:00+05:30 | FAC-JAI-01 |
| DRV006 | VEH015 | SHP1021 | ASSIGNED | 2026-08-04T14:00:00+05:30 | FAC-GGN-01 |
| DRV007 | VEH008 | SHP1007 | IN_TRANSIT | 2026-08-04T09:50:00+05:30 | FAC-JAI-01 |
| DRV007 | VEH007 | SHP1016 | IN_TRANSIT | 2026-08-04T12:00:00+05:30 | FAC-JAI-01 |

8 row(s)

### A10

```sql
SELECT dedupe_key, GROUP_CONCAT(exception_id || ':' || exception_status) AS rows
FROM driver_exceptions GROUP BY dedupe_key HAVING COUNT(*) > 1;
```

| dedupe_key | rows |
| --- | --- |
| DRV006-SHP1006-20260804-0934 | EXC001:NEEDS_INFORMATION,EXC009:DUPLICATE |

1 row(s)

### A11

```sql
SELECT a.appointment_id, s.shipment_id, s.current_status, s.vehicle_id, s2.shipment_id AS same_truck, fc.queue_state
FROM appointments a JOIN shipments s USING (shipment_id)
LEFT JOIN shipments s2 ON s2.vehicle_id = s.vehicle_id AND s2.shipment_id <> s.shipment_id
LEFT JOIN facility_checkins fc ON fc.shipment_id = s2.shipment_id
WHERE a.appointment_status = 'NO_SHOW';
```

| appointment_id | shipment_id | current_status | vehicle_id | same_truck | queue_state |
| --- | --- | --- | --- | --- | --- |
| APT1018 | SHP1018 | ASSIGNED | VEH003 | SHP1003 | WAITING_EARLY |

1 row(s)

### A12

```sql
SELECT v.shipment_id, v.effective_eta_ts, v.eta_confidence, v.eta_note, s.latest_eta_ts
FROM v_latest_eta v JOIN shipments s USING (shipment_id) WHERE v.eta_confidence = 'LOW';
```

| shipment_id | effective_eta_ts | eta_confidence | eta_note | latest_eta_ts |
| --- | --- | --- | --- | --- |
| SHP1013 | 2026-08-04T11:00:00+05:30 | LOW | Driver said only: late by one hour. | 2026-08-04T11:00:00+05:30 |
| SHP1017 | 2026-08-04T12:45:00+05:30 | LOW | Driver expects arrival between 12:30 and 13:00. | 2026-08-04T12:45:00+05:30 |

2 row(s)

### A13

```sql
SELECT operational_message_id, recipient_address, delivery_status FROM operational_messages
WHERE channel = 'EMAIL' AND recipient_address NOT IN (SELECT email FROM facility_contacts WHERE email IS NOT NULL);
SELECT contact_id, contact_role, contact_name FROM facility_contacts WHERE email IS NULL;
```

| operational_message_id | recipient_address | delivery_status |
| --- | --- | --- |
| OM004 | night.shift@setuhaul.example | FAILED |

1 row(s)

| contact_id | contact_role | contact_name |
| --- | --- | --- |
| CON005 | NIGHT_SHIFT_COORDINATOR | Vacant Position |

1 row(s)

### A14

```sql
SELECT v.shipment_id, v.effective_eta_ts, sl.slot_start_ts, sl.slot_end_ts, e.event_type
FROM v_inbound_operational_state v JOIN appointment_slots sl USING (slot_id)
LEFT JOIN dock_status_events e ON e.dock_id = sl.dock_id AND sl.slot_start_ts < e.event_end_ts AND sl.slot_end_ts > e.event_start_ts
WHERE v.effective_eta_ts > sl.slot_end_ts OR e.dock_event_id IS NOT NULL;
```

| shipment_id | effective_eta_ts | slot_start_ts | slot_end_ts | event_type |
| --- | --- | --- | --- | --- |
| SHP1002 | 2026-08-04T07:58:00+05:30 | 2026-08-04T08:00:00+05:30 | 2026-08-04T09:00:00+05:30 | CAPACITY_REDUCTION |
| SHP1004 | 2026-08-04T09:20:00+05:30 | 2026-08-04T09:00:00+05:30 | 2026-08-04T10:00:00+05:30 | CAPACITY_REDUCTION |
| SHP1005 | 2026-08-04T09:05:00+05:30 | 2026-08-04T09:00:00+05:30 | 2026-08-04T10:00:00+05:30 | BREAKDOWN |
| SHP1006 | 2026-08-04T11:20:00+05:30 | 2026-08-04T10:00:00+05:30 | 2026-08-04T11:00:00+05:30 |  |
| SHP1015 | 2026-08-04T18:30:00+05:30 | 2026-08-04T17:00:00+05:30 | 2026-08-04T18:00:00+05:30 |  |

5 row(s)

### A15

```sql
SELECT MIN(t) AS earliest, MAX(t) AS latest FROM (
  SELECT message_ts AS t FROM chat_messages UNION ALL SELECT created_at FROM eta_updates UNION ALL SELECT updated_at FROM appointments);
```

| earliest | latest |
| --- | --- |
| 2026-08-04T07:10:00+05:30 | 2026-08-04T16:30:00+05:30 |

1 row(s)

### A16

```sql
-- a booking whose unload runs past its slot overlaps the next slot on the same dock
SELECT a.appointment_id, s.shipment_id, d.dock_code, sl.slot_start_ts, s.expected_unload_min,
       nx.slot_id AS next_slot, nx.slot_start_ts AS next_start,
       (SELECT b.appointment_id FROM appointments b WHERE b.slot_id = nx.slot_id
          AND b.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')) AS next_booking
FROM appointments a JOIN shipments s USING (shipment_id) JOIN appointment_slots sl USING (slot_id)
JOIN docks d ON d.dock_id = sl.dock_id
JOIN appointment_slots nx ON nx.dock_id = sl.dock_id AND nx.slot_start_ts = sl.slot_end_ts
WHERE a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
  AND datetime(substr(sl.slot_start_ts,1,19), '+' || s.expected_unload_min || ' minutes') > datetime(substr(sl.slot_end_ts,1,19));
```

| appointment_id | shipment_id | dock_code | slot_start_ts | expected_unload_min | next_slot | next_start | next_booking |
| --- | --- | --- | --- | --- | --- | --- | --- |
| APT1014A | SHP1014 | D1 | 2026-08-04T11:00:00+05:30 | 75 | SLOT-JAI-005 | 2026-08-04T12:00:00+05:30 | APT1017 |
| APT1002 | SHP1002 | D2 | 2026-08-04T08:00:00+05:30 | 70 | SLOT-JAI-016 | 2026-08-04T09:00:00+05:30 | APT1004 |
| APT1005 | SHP1005 | D3 | 2026-08-04T09:00:00+05:30 | 75 | SLOT-JAI-031 | 2026-08-04T10:00:00+05:30 |  |
| APT1009 | SHP1009 | D4 | 2026-08-04T11:00:00+05:30 | 75 | SLOT-JAI-047 | 2026-08-04T12:00:00+05:30 |  |

4 row(s)

### A17

```sql
-- the same ETA appears in shipments.latest_eta_ts before (in sim time) the update that set it was made
SELECT s.shipment_id, s.original_eta_ts, s.latest_eta_ts, u.eta_update_id, u.created_at AS update_made_at
FROM shipments s JOIN eta_updates u ON u.shipment_id = s.shipment_id AND u.declared_eta_ts = s.latest_eta_ts
WHERE u.created_at > '2026-08-04T10:00:00+05:30';
```

| shipment_id | original_eta_ts | latest_eta_ts | eta_update_id | update_made_at |
| --- | --- | --- | --- | --- |
| SHP1015 | 2026-08-04T17:30:00+05:30 | 2026-08-04T18:30:00+05:30 | ETA010 | 2026-08-04T16:25:00+05:30 |

1 row(s)

### A18

```sql
-- revised ETAs on the shipment with no ETA update row behind them (no source, no confidence)
SELECT s.shipment_id, s.original_eta_ts, s.latest_eta_ts FROM shipments s
WHERE s.latest_eta_ts <> s.original_eta_ts AND NOT EXISTS (SELECT 1 FROM eta_updates u WHERE u.shipment_id = s.shipment_id);
```

| shipment_id | original_eta_ts | latest_eta_ts |
| --- | --- | --- |
| SHP1002 | 2026-08-04T07:55:00+05:30 | 2026-08-04T07:58:00+05:30 |
| SHP1009 | 2026-08-04T10:45:00+05:30 | 2026-08-04T10:50:00+05:30 |
| SHP1010 | 2026-08-04T10:50:00+05:30 | 2026-08-04T10:55:00+05:30 |
| SHP1011 | 2026-08-04T10:40:00+05:30 | 2026-08-04T10:45:00+05:30 |

4 row(s)

### A19

```sql
-- pending warehouse confirmations already past a sensible confirmation window at 10:00
SELECT a.appointment_id, a.shipment_id, a.booked_at, sl.slot_start_ts, a.is_current
FROM appointments a JOIN appointment_slots sl USING (slot_id) WHERE a.appointment_status = 'PENDING_CONFIRMATION';
```

| appointment_id | shipment_id | booked_at | slot_start_ts | is_current |
| --- | --- | --- | --- | --- |
| APT1013A | SHP1013 | 2026-08-04T09:32:00+05:30 | 2026-08-04T11:00:00+05:30 | 1 |
| APT1014A | SHP1014 | 2026-08-04T09:52:00+05:30 | 2026-08-04T11:00:00+05:30 | 1 |

2 row(s)

