"""Attach the scenario run's rubric checks to the LangSmith traces, and keep the turns as a regression dataset.

    python -m evals.score_scenarios            # reads docs/evidence/phase6_scenarios.json

For every driver message the scenario run sent, find its `driver_message` trace (by thread id + text) and add
feedback:
  no_false_promise      1 if the reply did not claim booked/confirmed before the booking service said CONFIRMED
  asked_one_question    1 if the reply asked at most one question (prompt rule 4)
  scenario_checks_pass  1 if every check of that scenario passed
Then upsert dataset `setuhaul-scenario-turns` (input text + scenario -> reply and booking stage) for regression runs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from langsmith import Client

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
DATASET = "setuhaul-scenario-turns"


def main() -> None:
    data = json.loads((ROOT / "docs" / "evidence" / "phase6_scenarios.json").read_text(encoding="utf-8"))
    project = os.getenv("LANGSMITH_PROJECT", "setuhaul")
    c = Client()
    passed = {s["id"]: all(ch["ok"] for ch in s["checks"]) for s in data["scenarios"]}
    found = scored = 0
    for r in data["replies"]:
        flt = (f'and(eq(name, "driver_message"), eq(metadata_key, "thread_id"), eq(metadata_value, "{r["thread"]}"))')
        runs = [x for x in c.list_runs(project_name=project, filter=flt, is_root=True)
                if (x.inputs or {}).get("text") == r["text"]]
        if not runs:
            continue
        found += 1
        run = sorted(runs, key=lambda x: x.start_time)[-1]
        c.create_feedback(run.id, key="no_false_promise", score=0 if r["false_promise"] else 1,
                          comment=f"stage after reply: {r['stage']}")
        c.create_feedback(run.id, key="asked_one_question", score=1 if r["reply"].count("?") <= 1 else 0)
        c.create_feedback(run.id, key="scenario_checks_pass", score=1 if passed.get(r["scenario"]) else 0,
                          comment=r["scenario"])
        scored += 1
    ds = next(iter(c.list_datasets(dataset_name=DATASET)), None) or c.create_dataset(
        DATASET, description="Driver turns from the scripted brief scenarios (steps 1-7) with the booking stage after "
                             "each reply. Re-run against new prompts/models and compare.")
    existing = {(e.inputs.get("thread"), e.inputs.get("text")) for e in c.list_examples(dataset_id=ds.id)}
    new = [r for r in data["replies"] if (r["thread"], r["text"]) not in existing]
    if new:
        c.create_examples(dataset_id=ds.id,
                          inputs=[{"text": r["text"], "scenario": r["scenario"], "thread": r["thread"], "user": r["user"]} for r in new],
                          outputs=[{"reply": r["reply"], "stage_after": r["stage"], "false_promise": r["false_promise"]} for r in new])
    print(f"traces matched {found}/{len(data['replies'])}, feedback written for {scored}; "
          f"dataset '{DATASET}' +{len(new)} examples")


if __name__ == "__main__":
    main()
