-- Handoff 3 "Schema additions", verbatim except PRAGMA journal_mode (set per connection).
-- The 18 supplied tables keep every existing column; only one column is ADDED
-- (appointment_slots.version) plus new tables, indexes and one trigger.

ALTER TABLE appointment_slots ADD COLUMN version INTEGER NOT NULL DEFAULT 0;

CREATE TABLE slot_holds (
  hold_id TEXT PRIMARY KEY, slot_id TEXT NOT NULL REFERENCES appointment_slots(slot_id),
  shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id), thread_id TEXT, quote_id TEXT,
  state TEXT NOT NULL CHECK (state IN ('ACTIVE','CONVERTED','EXPIRED','RELEASED')),
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE UNIQUE INDEX ux_active_hold_per_slot     ON slot_holds(slot_id)     WHERE state = 'ACTIVE';
CREATE UNIQUE INDEX ux_active_hold_per_shipment ON slot_holds(shipment_id) WHERE state = 'ACTIVE';

-- multi-slot bookings when unload time exceeds one slot (A6)
CREATE TABLE appointment_slot_claims (
  appointment_id TEXT NOT NULL REFERENCES appointments(appointment_id),
  slot_id TEXT NOT NULL REFERENCES appointment_slots(slot_id), active INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (appointment_id, slot_id));
CREATE UNIQUE INDEX ux_active_claim_per_slot ON appointment_slot_claims(slot_id) WHERE active = 1;

CREATE TABLE slot_quotes (quote_id TEXT PRIMARY KEY, thread_id TEXT, shipment_id TEXT,
  options_json TEXT NOT NULL, slot_versions_json TEXT NOT NULL, created_at TEXT, expires_at TEXT);
CREATE TABLE handoffs (handoff_id TEXT PRIMARY KEY, shipment_id TEXT, thread_id TEXT,
  reason_code TEXT NOT NULL, record_json TEXT NOT NULL, routed_to TEXT, channel_status TEXT,
  state TEXT NOT NULL, created_at TEXT, closed_at TEXT);
CREATE TABLE idempotency_keys (idem_key TEXT PRIMARY KEY, result_json TEXT, created_at TEXT);
CREATE TABLE audit_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT,
  action TEXT, entity_id TEXT, before_json TEXT, after_json TEXT);

-- duplicates (A10): one live exception per dedupe key
CREATE UNIQUE INDEX ux_exception_dedupe ON driver_exceptions(dedupe_key)
  WHERE exception_status NOT IN ('DUPLICATE','CANCELLED');

-- defence in depth: no booking on a slot that is not OPEN
CREATE TRIGGER trg_no_booking_on_closed_slot BEFORE INSERT ON appointments
WHEN (SELECT slot_status FROM appointment_slots WHERE slot_id = NEW.slot_id) <> 'OPEN'
BEGIN SELECT RAISE(ABORT, 'SLOT_NOT_OPEN'); END;
