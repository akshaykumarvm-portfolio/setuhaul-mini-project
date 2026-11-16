"""Decision 13: prove no double booking through the real HTTP API.

* 10 concurrent clients (threads, then separate processes) try to hold the SAME slot,
  100 rounds each with random jitter -> exactly 1 OK and 9 SLOT_TAKEN every round.
* 10 concurrent requests turn ONE hold into a booking -> exactly 1 pending appointment.
* DB guard: 10 writers bypass every application check and insert a claim on the same
  slot -> the partial unique indexes let exactly 1 through.
* After each scenario every invariant query returns no rows.

Holds are exclusive per slot, so "10 request_booking calls for one slot" can only
happen through one hold (or through a bug); both cases are covered.
"""
import multiprocessing as mp
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg
import pytest

from booking import bookings, clock
from booking.db import connect, write_tx
from tests.conftest import ROOT, add_synthetic_shipments, set_now
from tests.integrity import _client
from booking.invariants import check_all

ROUNDS = int(os.getenv("RACE_ROUNDS", "100"))
CLIENTS = 10
TARGET = ["SLOT-JAI-019"]          # D2 12:00-13:00, free in the snapshot


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server(db_url):
    """Run the booking service as a real uvicorn process against this test's database."""
    port = free_port()
    env = os.environ | {"DATABASE_URL": db_url, "SWEEPER_ENABLED": "0", "REDIS_URL": ""}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "booking.api:app", "--port", str(port),
                             "--log-level", "warning"], cwd=ROOT, env=env)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("booking service did not start")
    yield base
    proc.terminate()
    proc.wait(10)


@pytest.fixture()
def racers(conn):
    set_now(conn, "2026-08-04T10:00:00+05:30")   # frozen: holds never lapse mid-test
    return add_synthetic_shipments(conn, CLIENTS)


def quote_target(base, shipment_id) -> tuple[str, int]:
    q = httpx.post(f"{base}/quotes", json={"shipment_id": shipment_id}, timeout=30).json()
    return q["quote_id"], next(o["option_no"] for o in q["options"] if o["slot_ids"] == TARGET)


def assert_invariants(conn):
    bad = {k: v for k, v in check_all(conn).items() if v}
    assert not bad, bad


def run_hold_rounds(base, conn, shipments, mapper, label):
    tally = {"OK": 0, "SLOT_TAKEN": 0}
    for rnd in range(ROUNDS):
        quotes = [quote_target(base, s) for s in shipments]
        start_at = time.time() + 0.15
        results = mapper(_client.post_hold, [(base, q, n, start_at, 5.0) for q, n in quotes])
        codes = [c for _, c, _ in results]
        winners = [h for _, c, h in results if c == "OK"]
        assert len(winners) == 1, (label, rnd, codes)
        assert codes.count("SLOT_TAKEN") == CLIENTS - 1, (label, rnd, codes)
        tally["OK"] += 1
        tally["SLOT_TAKEN"] += CLIENTS - 1
        if rnd % 10 == 9:
            assert_invariants(conn)
        assert httpx.delete(f"{base}/holds/{winners[0]}", timeout=30).json()["status"] == "RELEASED"
    assert_invariants(conn)
    print(f"\n[{label}] {ROUNDS} rounds x {CLIENTS} clients: {tally['OK']} holds granted, "
          f"{tally['SLOT_TAKEN']} SLOT_TAKEN, 0 double holds; invariants clean")


def test_hold_race_threads(server, conn, racers):
    with ThreadPoolExecutor(CLIENTS) as pool:
        run_hold_rounds(server, conn, racers, lambda f, a: list(pool.map(f, a)), "threads")


def test_hold_race_processes(server, conn, racers):
    ctx = mp.get_context("spawn")
    with ctx.Pool(CLIENTS) as pool:
        run_hold_rounds(server, conn, racers, pool.map, "processes")


def test_request_race_one_hold(server, conn, racers):
    """10 clients race to turn the same hold into a booking (distinct idempotency keys)."""
    rounds = max(ROUNDS // 5, 5)
    created = 0
    with ThreadPoolExecutor(CLIENTS) as pool:
        for rnd in range(rounds):
            q, n = quote_target(server, racers[rnd % CLIENTS])
            hold = httpx.post(f"{server}/holds", json={"quote_id": q, "option_no": n}, timeout=30).json()
            start_at = time.time() + 0.15
            res = list(pool.map(_client.post_booking,
                                [(server, hold["hold_id"], f"r{rnd}-c{i}", start_at, 5.0) for i in range(CLIENTS)]))
            codes = [c for _, c, _ in res]
            appts = {a for _, c, a in res if c == "OK"}
            assert len(appts) == 1 and codes.count("ALREADY_REQUESTED") == CLIENTS - 1, (rnd, codes)
            created += 1
            assert_invariants(conn)
            appt_id = appts.pop()
            r = httpx.post(f"{server}/bookings/{appt_id}/reject", json={"reason": "race test reset"}, timeout=30)
            assert r.json()["status"] == "REJECTED"
    print(f"\n[request race] {rounds} rounds x {CLIENTS} clients: {created} bookings, "
          f"{created * (CLIENTS - 1)} ALREADY_REQUESTED; invariants clean")


def test_db_guard_rejects_second_claim_even_if_app_checks_are_skipped(db_url, conn, racers):
    """Simulate an application bug: skip holds and feasibility, insert claims directly."""
    set_now(conn, "2026-08-04T10:00:00+05:30")
    for rnd in range(max(ROUNDS // 5, 5)):
        barrier = threading.Barrier(CLIENTS)
        outcome = []

        def writer(sid):
            c = connect(db_url)
            try:
                barrier.wait()
                with c.transaction():
                    bookings.insert_pending(c, sid, TARGET, clock.now(c), replaces=None)
                outcome.append("OK")
            except psycopg.errors.UniqueViolation:
                outcome.append("SLOT_TAKEN")
            finally:
                c.close()

        threads = [threading.Thread(target=writer, args=(s,)) for s in racers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert outcome.count("OK") == 1 and outcome.count("SLOT_TAKEN") == CLIENTS - 1, (rnd, outcome)
        assert_invariants(conn)
        with write_tx(conn):  # reset for the next round
            ids = [r["appointment_id"] for r in conn.execute(
                "SELECT appointment_id FROM appointments WHERE slot_id = %s AND appointment_status = 'PENDING_CONFIRMATION'",
                (TARGET[0],))]
            conn.execute("UPDATE appointment_slot_claims SET active = 0 WHERE appointment_id = ANY(%s)", (ids,))
            conn.execute("UPDATE appointments SET appointment_status = 'CANCELLED' WHERE appointment_id = ANY(%s)", (ids,))
    print("\n[db guard] direct inserts bypassing app checks: exactly 1 claim per round; invariants clean")


def test_invariants_hold_on_fresh_snapshot(conn):
    assert_invariants(conn)
