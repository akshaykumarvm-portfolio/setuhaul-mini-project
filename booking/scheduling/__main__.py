"""python -m booking.scheduling --facility FAC-JAI-01 --at 10:00 [--out docs/evidence/phase5_comparison.md]"""
from __future__ import annotations

import argparse
from pathlib import Path

from booking import clock
from booking.db import connect, write_tx
from booking.scheduling import propose

COLS = [("weighted_lateness", "Priority-weighted lateness vs due time (min x weight)"), ("late_trucks", "Trucks later than due"),
        ("waiting_min", "Waiting after earliest dock start (min)"), ("promises_changed", "Promises changed"),
        ("unscheduled", "Trucks with no feasible slot"), ("needs_approval", "Needs coordinator approval"),
        ("utilisation_pct", "Dock utilisation now-22:00 (%)")]


def report(r: dict) -> str:
    plans = r["plans"]
    lines = [f"# Schedule comparison: {r['facility_id']} at {r['sim_now']} (simulated)", "",
             f"{r['trucks']} trucks still to unload. Due time = the promised slot, or the earliest dock start if the truck has none. "
             f"Weights: CRITICAL 8, HIGH 4, NORMAL 2, LOW 1. "
             f"CP-SAT: {plans['cpsat'].get('status')} in {plans['cpsat'].get('solve_s')} s"
             f"{' (proven optimal)' if plans['cpsat'].get('proven_optimal') else ''}. "
             f"Recommendation: **{plans[r['recommendation']]['label']}**.", "",
             "| Metric | " + " | ".join(v["label"] for v in plans.values()) + " |",
             "| --- |" + " --- |" * len(plans)]
    for key, label in COLS:
        lines.append(f"| {label} | " + " | ".join(str(v["metrics"][key]) for v in plans.values()) + " |")
    lines.append("| Rule violations | " + " | ".join(str(len(v["violations"])) for v in plans.values()) + " |")
    if plans["current"]["violations"]:
        lines += ["", "Why the current promises can't all hold:", ""] + [f"- {v}" for v in plans["current"]["violations"]]
    lines += ["", "## Per truck", "",
              "| Truck | Priority | ETA | Earliest dock | Unload | Promised | Dispatch rule | CP-SAT | CP-SAT late vs due (min) |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    base = {x["shipment_id"]: x for x in plans["dispatch_rule"]["rows"]}
    for x in plans["cpsat"]["rows"]:
        b = base[x["shipment_id"]]
        fmt = lambda y: f"{y['dock']} {y['start']}" + (" (approval)" if y["needs_approval"] else "") if y["dock"] else "none (hand-off)"
        lines.append(f"| {x['shipment_id']}{' (frozen)' if x['frozen'] else ''}{' ⚑' if x['flags'] else ''} | {x['priority']} | "
                     f"{x['eta']} | {x['earliest']} | {x['unload_min']} | {x['promised'] or '-'} | {fmt(b)} | {fmt(x)} | "
                     f"{x['late_min'] if x['late_min'] is not None else '-'} |")
    lines += ["", "⚑ = contradictory data (e.g. driver on two trucks): a person reviews before any booking.",
              "", r["note"]]
    return "\n".join(lines)


def sweep(conn, facility: str, times: list[str]) -> str:
    from booking.scheduling import cpsat, dispatch_rule
    from booking.scheduling.evaluate import metrics, objective, validate
    from booking.scheduling.problem import build_problem
    rows = ["| Sim time | Trucks | Weighted lateness: rule → CP-SAT | Waiting min: rule → CP-SAT | Objective: rule → CP-SAT | CP-SAT status (5 s) | Valid |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    for t in times:
        with write_tx(conn):
            clock.set_clock(conn, f"2026-08-04T{t}:00+05:30", rate=0)
        p = build_problem(conn, facility, clock.now(conn))
        b = dispatch_rule.solve(p)["assignments"]
        r = cpsat.solve(p, time_limit_s=5, hint=b)
        mb, mc = metrics(p, b), metrics(p, r["assignments"])
        ob, oc = objective(p, b), objective(p, r["assignments"])
        ok = not validate(p, b) and not validate(p, r["assignments"])
        mark = lambda x, y: f"{x} → **{y}**" if y < x else f"{x} → {y}"
        rows.append(f"| {t} | {len(p.jobs)} | {mark(mb['weighted_lateness'], mc['weighted_lateness'])} | "
                    f"{mark(mb['waiting_min'], mc['waiting_min'])} | {mark(ob, oc)} | {r['status']} {r['solve_s']}s | {'yes' if ok else 'NO'} |")
    return "\n".join(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--facility", default="FAC-JAI-01")
    ap.add_argument("--at", default=None, help="HH:MM on 2026-08-04 (sets and freezes the simulated clock)")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--sweep", help="comma-separated HH:MM times for a baseline-vs-CP-SAT table")
    a = ap.parse_args()
    conn = connect()
    if a.at:
        with write_tx(conn):
            clock.set_clock(conn, f"2026-08-04T{a.at}:00+05:30", rate=0)
    text = report(propose(conn, a.facility, clock.now(conn)))
    if a.sweep:
        table = sweep(conn, a.facility, a.sweep.split(","))
        text = table if a.at is None else f"{text}\n\n## Across the day\n\n{table}"
    print(text)
    if a.out:
        a.out.write_text(text + "\n", encoding="utf-8")
