"""Deterministic guardrails around the LLM. Each test scripts what a (bad or good) model would say."""
import uuid

from agent.extraction import DriverMessage, resolve_eta
from agent.timeutil import parse_ts
from tests.agent.conftest import ai
from agent.intake import Intake


def turn(intake, driver, text, thread):
    return intake.handle(driver, text, thread_id=thread, message_id=uuid.uuid4().hex)


def test_happy_path_hold_and_request_through_tools(make_intake, booking_api):
    intake, _ = make_intake([
        ai("", ("get_driver_context", {})),
        ai("", ("link_shipment", {"shipment_ref": "SHP1006"})),
        ai("", ("find_slot_options", {})),
        ai("1. D2 12:00-12:45. Not reserved yet. Want me to hold it?"),
        ai("", ("hold_option", {"option_no": 1})),
        ai("Holding D2 at 12:00 for 5 minutes. Shall I request it?"),
        ai("", ("request_booking", {})),
        ai("Requested, waiting for the warehouse. Keep your current slot until I confirm."),
    ])
    r1 = turn(intake, "DRV006", "Running late, show me slots", "T1")
    assert "D2" in r1["reply"] and r1["state"]["quote_id"]
    r2 = turn(intake, "DRV006", "Option 1", "T1")
    assert r2["state"]["hold_id"]
    r3 = turn(intake, "DRV006", "yes", "T1")
    assert r3["state"]["appointment_id"]
    st = booking_api.get("/shipments/SHP1006/status").json()
    assert st["stage"] == "PENDING" and st["current_appointment"]["appointment_id"] == "APT1006"
    # transcript stored through the booking service
    msgs = booking_api.get("/threads/T1/messages").json()["messages"]
    assert [m["sender_type"] for m in msgs] == ["DRIVER", "AGENT"] * 3


def test_post_check_blocks_false_booking_claim(make_intake, booking_api):
    intake, _ = make_intake([
        ai("", ("link_shipment", {"shipment_ref": "SHP1006"})),
        ai("", ("find_slot_options", {})),
        ai("1. D2 12:00. Not reserved yet. Which one?"),
        ai("", ("hold_option", {"option_no": 1})),
        ai("Great news, you're booked on D2 at 12:00!"),          # a lie: it's only a hold
    ])
    turn(intake, "DRV006", "book me the earliest", "T2")
    r = turn(intake, "DRV006", "option 1", "T2")
    assert "booked" not in r["reply"].lower() or "not booked" in r["reply"].lower()
    assert "holding" in r["reply"].lower() and "not booked yet" in r["reply"].lower()


def test_post_check_blocks_invented_dock(make_intake):
    intake, _ = make_intake([
        ai("", ("link_shipment", {"shipment_ref": "SHP1006"})),
        ai("D9 is free at 11:00, want it?"),                       # no tool ever returned D9
    ])
    r = turn(intake, "DRV006", "any slot?", "T3")
    assert "D9" not in r["reply"]


def test_write_guard_refuses_shipment_tools_before_link(make_intake, booking_api):
    intake, _ = make_intake([
        ai("", ("find_slot_options", {})),                         # skipped linking
        ai("Which load is this about?"),
    ])
    r = turn(intake, "DRV004", "late", "T4")
    assert r["reply"] == "Which load is this about?"
    assert booking_api.get("/handoffs").json()["handoffs"] == []
    from booking.db import connect
    import os
    c = connect(os.environ["DATABASE_URL"])
    assert c.execute("SELECT COUNT(*) AS n FROM slot_quotes").fetchone()["n"] == 0
    c.close()


def test_model_cannot_raise_eta_confidence_above_code(make_intake, booking_api):
    intake, _ = make_intake([
        ai("", ("link_shipment", {"shipment_ref": "SHP1013"})),
        ai("", ("record_eta", {"eta_lo": "12:00", "eta_hi": "12:00", "confidence": "HIGH"})),
        ai("Noted. Does that mean you'll reach the gate around 12:00?"),
    ])
    hint = resolve_eta(DriverMessage(intent="REPORT_DELAY", delay_minutes=60), parse_ts("2026-08-04T10:00:00+05:30"),
                       parse_ts("2026-08-04T11:00:00+05:30")).model_dump()
    booking_api.post("/threads", json={"driver_id": "DRV013", "thread_id": "T5"})
    out = intake.agent.invoke({"messages": [("human", "I am late by one hour.")], "eta_hint": hint},
                              config={"configurable": {"thread_id": "T5"}},
                              context=__import__("agent.tools", fromlist=["AgentContext"]).AgentContext(
                                  driver_id="DRV013", thread_id="T5", client=intake.client_factory("DRV013"),
                                  sim_now="2026-08-04T10:00:00+05:30"))
    assert out["eta_confidence"] == "LOW"
    assert booking_api.get("/shipments/SHP1013/status").json()["eta"]["confidence"] == "LOW"


def test_hold_refused_while_eta_is_low(make_intake, booking_api):
    intake, _ = make_intake([
        ai("", ("link_shipment", {"shipment_ref": "SHP1013"})),
        ai("", ("find_slot_options", {})),                          # SHP1013's seeded ETA is LOW
        ai("", ("hold_option", {"option_no": 1})),
        ai("What time will you reach the gate?"),
    ])
    turn(intake, "DRV013", "hold the first one", "T6")
    assert booking_api.get("/shipments/SHP1013/status").json()["hold"] is None


def test_no_feasible_slot_creates_handoff_in_code(make_intake, booking_api):
    booking_api.post("/admin/clock", json={"sim_ts": "2026-08-04T16:30:00+05:30", "rate": 0})
    intake, _ = make_intake([
        ai("", ("link_shipment", {"shipment_ref": "SHP1015"})),
        ai("", ("record_eta", {"eta_lo": "18:30", "eta_hi": "18:30", "confidence": "HIGH"})),
        ai("", ("find_slot_options", {})),
        ai("Nothing fits after 18:30; D5 is in maintenance until 22:00. A coordinator will call you."),
    ])
    r = turn(intake, "DRV015", "reaching 18:30, need a slot", "T7")
    hs = booking_api.get("/handoffs").json()["handoffs"]
    assert any(h["shipment_id"] == "SHP1015" and h["reason_code"] == "NO_FEASIBLE_SLOT" for h in hs)
    assert r["state"]["escalated"] is True


def test_promised_handoff_is_made_true(make_intake, booking_api):
    intake, _ = make_intake([
        ai("", ("link_shipment", {"shipment_ref": "SHP1006"})),
        ai("I'll pass this to the coordinator right away."),       # model forgot to call escalate
    ])
    turn(intake, "DRV006", "my brakes feel wrong", "T8")
    assert any(h["shipment_id"] == "SHP1006" for h in booking_api.get("/handoffs").json()["handoffs"])


def test_two_failed_clarifications_escalate(make_intake, booking_api):
    intake, _ = make_intake([
        ai("", ("link_shipment", {"shipment_ref": "SHP1006"})),
        ai("What time will you reach the gate?"),
        ai("Sorry, what time do you expect to arrive?"),
        ai("Could you give me a time, like 11:30?"),
    ])
    turn(intake, "DRV006", "late", "T9")
    turn(intake, "DRV006", "dunno", "T9")
    r = turn(intake, "DRV006", "hmm", "T9")
    assert "coordinator" in r["reply"].lower()
    assert any(h["reason_code"] == "CLARIFICATION_FAILED" for h in booking_api.get("/handoffs").json()["handoffs"])


def test_duplicate_messages_answered_once(make_intake):
    intake, model = make_intake([ai("Got it. Which load?")])
    first = intake.handle("DRV006", "Traffic after Shahpura. Reaching around 11:20.", thread_id="T10", message_id="wa-t-1")
    again = intake.handle("DRV006", "Traffic after Shahpura. Reaching around 11:20.", thread_id="T10", message_id="wa-t-1")
    retry = intake.handle("DRV006", "Traffic after  shahpura. Reaching around 11:20.", thread_id="T10", message_id="wa-t-2")
    seeded = intake.handle("DRV006", "a different text", thread_id="T10", message_id="wa-9001")  # id already in the DB
    assert not first["duplicate"] and again["duplicate"] and retry["duplicate"]   # THR001 / THR009
    assert again["reply"] == retry["reply"] == first["reply"]
    assert seeded["duplicate"]                     # the DB unique index catches it even past Redis


def test_driver_cannot_link_someone_elses_load(make_intake):
    intake, _ = make_intake([
        ai("", ("link_shipment", {"shipment_ref": "SHP1014"})),    # belongs to DRV014
        ai("That load isn't assigned to you."),
    ])
    r = turn(intake, "DRV006", "link SHP1014", "T11")
    assert not r["state"]["shipment_id"]


def test_conversation_state_survives_restart(make_intake, client_factory, fake_redis):
    import fakeredis

    from agent.checkpointer import RedisSaver
    from agent.graph import build_agent
    from agent.intake import Intake
    from tests.agent.conftest import ScriptedModel
    server = fakeredis.FakeServer()
    script = [ai("", ("link_shipment", {"shipment_ref": "SHP1006"})), ai("Linked. What time will you arrive?"),
              ai("Thanks.")]
    m = ScriptedModel(script=script)
    saver1 = RedisSaver(fakeredis.FakeRedis(server=server))
    a1 = Intake(build_agent(checkpointer=saver1, model=m, fallback_model=False, summarizer=m), redis_client=fake_redis,
                client_factory=client_factory)
    a1.handle("DRV006", "late", thread_id="T12", message_id="m1")
    saver2 = RedisSaver(fakeredis.FakeRedis(server=server))           # "new process", same Redis
    a2 = Intake(build_agent(checkpointer=saver2, model=m, fallback_model=False, summarizer=m), redis_client=fake_redis,
                client_factory=client_factory)
    assert a2.agent.get_state({"configurable": {"thread_id": "T12"}}).values["shipment_id"] == "SHP1006"


def test_eta_resolution_rules():
    now, plan = parse_ts("2026-08-04T10:00:00+05:30"), parse_ts("2026-08-04T11:00:00+05:30")
    r = resolve_eta(DriverMessage(intent="REPORT_DELAY", arrival_time="11:20"), now, plan)
    assert (r.eta_hi[11:16], r.confidence) == ("11:20", "HIGH")
    r = resolve_eta(DriverMessage(intent="REPORT_DELAY", arrival_window_start="12:30", arrival_window_end="13:00"), now, plan)
    assert (r.eta_lo[11:16], r.eta_hi[11:16], r.confidence) == ("12:30", "13:00", "MEDIUM")
    r = resolve_eta(DriverMessage(intent="REPORT_DELAY", delay_minutes=60), now, plan)
    assert (r.eta_hi[11:16], r.confidence) == ("12:00", "LOW") and r.needs_question
    r = resolve_eta(DriverMessage(intent="REPORT_DELAY", arrival_time="10:30", is_repair_estimate=True), now, plan)
    assert r.confidence == "LOW" and "reach the gate" in r.needs_question


def test_cannot_hold_options_the_driver_has_not_seen(make_intake, booking_api):
    intake, _ = make_intake([
        ai("", ("link_shipment", {"shipment_ref": "SHP1006"})),
        ai("", ("find_slot_options", {})),
        ai("", ("hold_option", {"option_no": 1})),                 # same turn: driver never saw the list
        ai("Here are the options: 1. D2 12:00. Not reserved. Which one?"),
    ])
    turn(intake, "DRV006", "option 1 please", "T13")
    assert booking_api.get("/shipments/SHP1006/status").json()["hold"] is None


def test_options_are_listed_even_if_the_model_forgets(make_intake, booking_api):
    intake, _ = make_intake([
        ai("", ("link_shipment", {"shipment_ref": "SHP1006"})),
        ai("", ("find_slot_options", {})),
        ai("Your slot no longer fits. Would you like to see other options?"),   # forgets to list them
    ])
    r = turn(intake, "DRV006", "late, reaching 11:20", "T14")
    assert "Options (not reserved yet):" in r["reply"] and "1. Dock" in r["reply"]


def test_record_eta_fetches_options_when_current_slot_no_longer_fits(make_intake, booking_api):
    intake, _ = make_intake([
        ai("", ("link_shipment", {"shipment_ref": "SHP1006"})),
        ai("", ("record_eta", {"eta_lo": "11:20", "eta_hi": "11:20", "confidence": "HIGH"})),
        ai("Noted, your 10:00 slot no longer fits."),
        ai("", ("hold_option", {"option_no": 1})),
        ai("Held. Shall I request it?"),
    ])
    r1 = turn(intake, "DRV006", "Reaching the gate at 11:20", "T15")
    assert "1. Dock" in r1["reply"] and r1["state"]["quote_id"]
    turn(intake, "DRV006", "option 1", "T15")
    assert booking_api.get("/shipments/SHP1006/status").json()["hold"] is not None


def test_redis_checkpointer_keeps_every_channel_like_inmemory(client_factory, fake_redis):
    """Regression: LangGraph passes only changed channels to put(); values set early in a turn (facts, eta_hint)
    must survive to the next turn exactly as with LangGraph's own InMemorySaver."""
    import fakeredis
    from langgraph.checkpoint.memory import InMemorySaver

    from agent.checkpointer import RedisSaver
    from agent.graph import build_agent
    from tests.agent.conftest import ScriptedModel

    def script():
        return [ai("", ("link_shipment", {"shipment_ref": "SHP1006"})), ai("What time will you reach the gate?"),
                ai("Thanks, noted.")]

    states = []
    for saver in (InMemorySaver(), RedisSaver(fakeredis.FakeRedis())):
        m = ScriptedModel(script=script())
        intake = Intake(build_agent(checkpointer=saver, model=m, fallback_model=False, summarizer=m),
                        redis_client=None, client_factory=client_factory)
        agent = intake.agent
        cfg = {"configurable": {"thread_id": f"CK-{type(saver).__name__}"}}
        from agent.tools import AgentContext
        ctx = AgentContext(driver_id="DRV006", thread_id=cfg["configurable"]["thread_id"],
                           client=client_factory("DRV006"), sim_now="2026-08-04T10:00:00+05:30")
        client_factory("DRV006").open_thread("DRV006", cfg["configurable"]["thread_id"])
        agent.invoke({"messages": [("human", "late")], "facts": {"delay_minutes": 60}, "eta_hint": {"eta_lo": "2026-08-04T11:00:00+05:30", "eta_hi": "2026-08-04T11:00:00+05:30",
                                  "confidence": "LOW", "basis": "relative delay", "needs_question": None}},
                     config=cfg, context=ctx)
        agent.invoke({"messages": [("human", "ok")]}, config=cfg, context=ctx)
        v = agent.get_state(cfg).values
        states.append({k: v.get(k) for k in ("shipment_id", "facts", "eta_hint", "clarify_count")} | {"n": len(v["messages"])})
    assert states[0] == states[1]
    assert states[1]["facts"] == {"delay_minutes": 60} and states[1]["shipment_id"] == "SHP1006"


def test_extraction_outage_fails_safe(make_intake, booking_api):
    """Live finding: an OpenRouter 429 on extraction left no code-resolved window and the model recorded a vague
    delay at MEDIUM. Without a window, only a stated clock time may be recorded above LOW."""
    intake, _ = make_intake([
        ai("", ("link_shipment", {"shipment_ref": "SHP1013"})),
        ai("", ("record_eta", {"eta_lo": "12:00", "eta_hi": "12:00", "confidence": "HIGH"})),
        ai("Does that mean you'll reach the gate around 12:00?"),
        ai("", ("record_eta", {"eta_lo": "11:10", "eta_hi": "11:10", "confidence": "HIGH"})),
        ai("Noted, 11:10."),
    ])
    turn(intake, "DRV013", "late by one hour", "T16")              # no extraction model here: no code window at all
    assert booking_api.get("/shipments/SHP1013/status").json()["eta"]["confidence"] == "LOW"
    turn(intake, "DRV013", "I'll be at the gate at 11:10", "T16")
    eta = booking_api.get("/shipments/SHP1013/status").json()["eta"]
    assert eta["confidence"] == "MEDIUM" and eta["eta_hi"][11:16] == "11:10"


def test_explicit_time_detector():
    from agent.extraction import has_explicit_time
    assert has_explicit_time("reaching the gate at 11:10") and has_explicit_time("around 6 pm") and has_explicit_time("by 18.30")
    assert not has_explicit_time("late by 45 minutes") and not has_explicit_time("maybe an hour more")
