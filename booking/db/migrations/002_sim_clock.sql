-- Simulated clock shared by every process (A15). Single row, id = 1.
CREATE TABLE sim_clock (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  anchor_sim TEXT NOT NULL,      -- simulated time at the anchor (+05:30)
  anchor_real TEXT NOT NULL,     -- wall-clock UTC at the anchor
  rate REAL NOT NULL DEFAULT 1.0 -- simulated seconds per real second
);
