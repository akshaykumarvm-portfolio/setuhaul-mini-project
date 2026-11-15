-- ETA as a window + confidence (Decision 3). eta_updates keeps one declared time
-- (we store the window's late edge there); the window itself lives here.
CREATE TABLE eta_windows (
  eta_update_id TEXT PRIMARY KEY REFERENCES eta_updates(eta_update_id),
  shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
  eta_lo TEXT NOT NULL, eta_hi TEXT NOT NULL,
  confidence TEXT NOT NULL CHECK (confidence IN ('LOW','MEDIUM','HIGH')),
  cause TEXT, cause_fixed INTEGER CHECK (cause_fixed IS NULL OR cause_fixed IN (0,1)),
  source_text TEXT,
  CHECK (eta_hi >= eta_lo));

-- Every active appointment gets a claim row for each slot it occupies, so one unique
-- index (ux_active_claim_per_slot) guards single- and multi-slot bookings alike.
-- Backfill the supplied active appointments (one slot each).
INSERT INTO appointment_slot_claims (appointment_id, slot_id, active)
SELECT appointment_id, slot_id, 1 FROM appointments
WHERE appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS');
