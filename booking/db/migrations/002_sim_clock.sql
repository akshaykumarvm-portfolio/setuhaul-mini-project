-- Simulated clock shared by every process (A15). Single row, id = 1.
-- sim_now = anchor_sim + (clock_timestamp() - anchor_real) * rate, computed by the DB,
-- so every client agrees regardless of its own wall clock.
CREATE TABLE sim_clock (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  anchor_sim TIMESTAMPTZ NOT NULL,
  anchor_real TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  rate DOUBLE PRECISION NOT NULL DEFAULT 1.0
);
