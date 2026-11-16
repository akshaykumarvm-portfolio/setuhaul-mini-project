import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, eventsUrl, post } from "../lib/api";
import { Session } from "../lib/auth";
import { countdown, hhmm, label, secondsBetween } from "../lib/time";
import Brand from "../components/Brand";
import Timeline, { Dock, DockEvent, SlotRow } from "./Timeline";

interface Booking {
  appointment_id: string;
  shipment_id: string;
  order_reference: string;
  priority: string;
  driver_id: string;
  dock_code: string;
  start: string;
  end: string;
  booked_at: string;
  expires_at?: string;
  replaces: string | null;
  replaces_view?: { dock_code: string; start: string; end: string; status: string } | null;
  load_weight_kg: number;
  source: string;
}
interface Handoff {
  handoff_id: string;
  shipment_id: string;
  reason_code: string;
  routed_to: string | null;
  channel_status: string;
  created_at: string;
  record: any;
}
interface Feed {
  at: string;
  text: string;
}

const FACILITIES = [
  { id: "FAC-JAI-01", name: "Jaipur DC" },
  { id: "FAC-GGN-01", name: "Gurugram DC" },
];

function eventText(e: any): string {
  switch (e.type) {
    case "SLOT_HELD":
      return `Slot held (${e.slot_ids?.join(", ")})`;
    case "SLOT_REQUESTED":
      return `New request ${e.appointment_id}`;
    case "BOOKING_CONFIRMED":
      return `Confirmed ${e.appointment_id}`;
    case "SLOT_FREED":
      return `Freed ${e.slot_ids?.join(", ")}${e.reoffer?.length ? ` · re-offer to ${e.reoffer.map((r: any) => r.shipment_id).join(", ")}` : ""}`;
    case "HANDOFF":
      return `Hand-off ${e.handoff_id}`;
    default:
      return label(e.type || "update");
  }
}

export default function OpsConsole({ session, onSignOut }: { session: Session; onSignOut: () => void }) {
  const [facility, setFacility] = useState(session.facilityId || "FAC-JAI-01");
  const [tl, setTl] = useState<{ sim_now: string; docks: Dock[]; slots: SlotRow[]; dock_events: DockEvent[] } | null>(null);
  const [pending, setPending] = useState<Booking[]>([]);
  const [handoffs, setHandoffs] = useState<Handoff[]>([]);
  const [feed, setFeed] = useState<Feed[]>([]);
  const [live, setLive] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openHof, setOpenHof] = useState<string | null>(null);
  const [flash, setFlash] = useState<Set<string>>(new Set());
  const [tick, setTick] = useState(0);
  const lastRefresh = useRef(0);

  const isPlanner = session.role === "planner";

  const refresh = useCallback(async () => {
    lastRefresh.current = Date.now();
    try {
      const [t, b, h] = await Promise.all([
        api(`/facilities/${facility}/timeline`),
        api(`/bookings?status=PENDING_CONFIRMATION&facility_id=${facility}`),
        api(`/handoffs?state=OPEN&facility_id=${facility}`),
      ]);
      setTl(t);
      setPending(b.bookings || []);
      setHandoffs(h.handoffs || []);
      setError(null);
    } catch (e: any) {
      setError(e.message);
    }
  }, [facility]);

  // initial load + safety-net polling
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 10000);
    return () => clearInterval(t);
  }, [refresh]);

  // 1 s tick for countdowns
  useEffect(() => {
    const t = setInterval(() => setTick((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, []);

  // live updates (server-sent events fed by Redis pub/sub)
  useEffect(() => {
    let es: EventSource | null = null;
    let closed = false;
    eventsUrl(facility, session).then((url) => {
      if (closed) return;
      es = new EventSource(url);
      es.onopen = () => setLive(true);
      es.onerror = () => setLive(false);
      es.onmessage = (m) => {
        try {
          const e = JSON.parse(m.data);
          setFeed((f) => [{ at: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), text: eventText(e) }, ...f].slice(0, 30));
          if (e.appointment_id) setFlash(new Set([e.appointment_id]));
        } catch {
          /* ignore malformed */
        }
        if (Date.now() - lastRefresh.current > 500) refresh();
      };
    });
    return () => {
      closed = true;
      es?.close();
    };
  }, [facility, session, refresh]);

  // sim time advances in real time between refreshes when the clock is running; countdowns use the server's now
  const simNow = tl?.sim_now || "";

  const act = async (key: string, fn: () => Promise<any>) => {
    setBusy(key);
    try {
      const r = await fn();
      if (r && r.ok === false) setError(`${r.code}: ${r.message || ""}`);
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  };

  const confirm = (b: Booking) => act(b.appointment_id, () => post(`/bookings/${b.appointment_id}/confirm`, {}));
  const reject = (b: Booking) => {
    const reason = window.prompt(`Reject ${b.shipment_id} on ${b.dock_code} ${hhmm(b.start)}? Reason for the driver:`, "Dock crew unavailable at that time");
    if (reason) act(b.appointment_id, () => post(`/bookings/${b.appointment_id}/reject`, { reason }));
  };
  const closeHof = (h: Handoff) => {
    const resolution = window.prompt(`Resolve ${h.handoff_id} for ${h.shipment_id}. What did you do?`, "Called driver; rebooked manually");
    if (resolution) act(h.handoff_id, () => post(`/handoffs/${h.handoff_id}/close`, { resolution }));
  };
  const clock = (body: object) => act("clock", () => post("/admin/clock", body));

  const kpis = useMemo(() => {
    const slots = tl?.slots || [];
    const nowM = simNow ? Date.parse(simNow) : 0;
    const down = (tl?.dock_events || []).filter(
      (e) => e.event_type !== "REOPENED" && Date.parse(e.event_start_ts) <= nowM && (!e.event_end_ts || Date.parse(e.event_end_ts) > nowM),
    ).length;
    return {
      pending: pending.length,
      handoffs: handoffs.length,
      held: new Set(slots.filter((s) => s.hold_id).map((s) => s.hold_id)).size,
      free: slots.filter((s) => s.slot_status === "OPEN" && !s.appointment_id && !s.hold_id && s.slot_start_ts > simNow).length,
      down,
    };
  }, [tl, pending, handoffs, simNow]);

  void tick;

  return (
    <div className="ops-shell">
      <header className="topbar dark">
        <Brand compact />
        <div className="topbar-mid">
          {isPlanner ? (
            <select value={facility} onChange={(e) => setFacility(e.target.value)} aria-label="Facility">
              {FACILITIES.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.name}
                </option>
              ))}
            </select>
          ) : (
            <span className="fac">{FACILITIES.find((f) => f.id === facility)?.name}</span>
          )}
          <span className={`live ${live ? "on" : ""}`}>{live ? "Live" : "Polling"}</span>
        </div>
        <div className="topbar-right">
          <span className="clock" title="Simulated clock (snapshot 4 Aug 2026)">
            <small>Sim time</small> <b className="mono">{hhmm(simNow)}</b>
          </span>
          <span className="who">
            {session.username} · {session.role}
          </span>
          <button className="btn ghost sm light" onClick={onSignOut}>
            Sign out
          </button>
        </div>
      </header>

      {error && (
        <div className="banner" onClick={() => setError(null)}>
          {error} <span className="muted">(dismiss)</span>
        </div>
      )}

      <section className="kpis">
        <div className={`kpi ${kpis.pending ? "warn" : ""}`}>
          <b>{kpis.pending}</b>
          <span>awaiting confirmation</span>
        </div>
        <div className={`kpi ${kpis.handoffs ? "alert" : ""}`}>
          <b>{kpis.handoffs}</b>
          <span>open hand-offs</span>
        </div>
        <div className="kpi">
          <b>{kpis.held}</b>
          <span>slots on hold</span>
        </div>
        <div className="kpi">
          <b>{kpis.free}</b>
          <span>free slots ahead</span>
        </div>
        <div className={`kpi ${kpis.down ? "alert" : ""}`}>
          <b>{kpis.down}</b>
          <span>docks impaired now</span>
        </div>
        {isPlanner && (
          <div className="clockctl">
            <span className="eyebrow">Sim clock</span>
            <div className="btn-row">
              <button className="btn sm" disabled={busy === "clock"} onClick={() => clock({ sim_ts: "2026-08-04T10:00:00+05:30", rate: 0 })}>
                10:00
              </button>
              <button className="btn sm" disabled={busy === "clock"} onClick={() => clock({ sim_ts: "2026-08-04T16:30:00+05:30", rate: 0 })}>
                16:30
              </button>
              <button className="btn sm" disabled={busy === "clock"} onClick={() => clock({ advance_min: 5 })}>
                +5 min
              </button>
              <button className="btn sm" disabled={busy === "clock"} onClick={() => clock({ advance_min: 15 })}>
                +15 min
              </button>
              <button className="btn sm" disabled={busy === "clock"} onClick={() => act("sweep", () => post("/admin/sweep"))}>
                Run sweeper
              </button>
            </div>
          </div>
        )}
      </section>

      <main className="ops-main">
        <section className="panel timeline-panel">
          <div className="panel-head">
            <h2>Dock timeline</h2>
            <span className="muted sm">Hover a block for details · {tl?.slots.length || 0} slots</span>
          </div>
          {tl ? <Timeline docks={tl.docks} slots={tl.slots} events={tl.dock_events} simNow={simNow} highlight={flash} /> : <div className="empty">Loading…</div>}
          {feed.length > 0 && (
            <div className="feed">
              <div className="eyebrow">Live activity</div>
              {feed.map((f, i) => (
                <div key={i}>
                  <span className="mono muted">{f.at}</span> {f.text}
                </div>
              ))}
            </div>
          )}
        </section>

        <aside className="side">
          <section className="panel">
            <div className="panel-head">
              <h2>Awaiting your confirmation</h2>
              <span className="count">{pending.length}</span>
            </div>
            {pending.length === 0 && <div className="empty">No requests waiting.</div>}
            {pending.map((b) => {
              const secs = b.expires_at && simNow ? secondsBetween(simNow, b.expires_at) : null;
              return (
                <article key={b.appointment_id} className={`req ${secs !== null && secs < 300 ? "urgent" : ""}`}>
                  <div className="req-top">
                    <span className="mono">{b.shipment_id}</span>
                    <span className={`badge pr-${b.priority.toLowerCase()}`}>{b.priority}</span>
                    <span className="muted sm">{(b.load_weight_kg / 1000).toFixed(1)} t</span>
                  </div>
                  <div className="req-move">
                    {b.replaces_view ? (
                      <>
                        <span className="from">
                          {b.replaces_view.dock_code} {hhmm(b.replaces_view.start)}
                        </span>
                        <span className="arrow">→</span>
                      </>
                    ) : (
                      <span className="from muted">no slot</span>
                    )}
                    <span className="to">
                      {b.dock_code} {hhmm(b.start)}–{hhmm(b.end)}
                    </span>
                  </div>
                  <div className="req-meta muted sm">
                    {b.driver_id} · via {label(b.source)} · requested {hhmm(b.booked_at)}
                    {secs !== null &&
                      (secs > 0 ? (
                        <>
                          {" "}
                          · lapses in <b>{countdown(simNow, b.expires_at!)}</b>
                        </>
                      ) : (
                        <b> · lapsed, closing on next sweep</b>
                      ))}
                  </div>
                  <div className="btn-row">
                    <button className="btn primary sm" disabled={busy === b.appointment_id} onClick={() => confirm(b)}>
                      Confirm
                    </button>
                    <button className="btn sm" disabled={busy === b.appointment_id} onClick={() => reject(b)}>
                      Reject
                    </button>
                  </div>
                </article>
              );
            })}
          </section>

          <section className="panel">
            <div className="panel-head">
              <h2>Hand-offs</h2>
              <span className="count alert">{handoffs.length}</span>
            </div>
            {handoffs.length === 0 && <div className="empty">Nothing needs a person right now.</div>}
            {handoffs.map((h) => {
              const r = h.record || {};
              const open = openHof === h.handoff_id;
              return (
                <article key={h.handoff_id} className="hof">
                  <button className="hof-head" onClick={() => setOpenHof(open ? null : h.handoff_id)} aria-expanded={open}>
                    <span className={`reason r-${h.reason_code.toLowerCase()}`}>{label(h.reason_code)}</span>
                    <span className="mono">{h.shipment_id}</span>
                    <span className="muted sm">{hhmm(h.created_at)}</span>
                  </button>
                  {r.summary && <p className="hof-sum">{r.summary}</p>}
                  {open && (
                    <div className="hof-body">
                      <dl>
                        <dt>Driver</dt>
                        <dd>
                          {r.driver?.driver_name} · {r.driver?.phone}
                        </dd>
                        <dt>Load</dt>
                        <dd>
                          {r.shipment?.priority_code} · {r.shipment?.required_dock_type?.toLowerCase()} ·{" "}
                          {((r.shipment?.load_weight_kg || 0) / 1000).toFixed(1)} t · {r.shipment?.expected_unload_min} min
                        </dd>
                        <dt>ETA</dt>
                        <dd>
                          {hhmm(r.eta?.eta_lo) === hhmm(r.eta?.eta_hi) ? hhmm(r.eta?.eta_hi) : `${hhmm(r.eta?.eta_lo)}–${hhmm(r.eta?.eta_hi)}`} (
                          {r.eta?.confidence}) · earliest dock {hhmm(r.earliest_dock_start)}
                        </dd>
                        <dt>Current slot</dt>
                        <dd>
                          {r.current_appointment
                            ? `${r.current_appointment.dock_code} ${hhmm(r.current_appointment.slot_start_ts)} (${label(r.current_appointment.appointment_status)})`
                            : "none"}
                        </dd>
                        <dt>Routed to</dt>
                        <dd>{h.routed_to || "—"}</dd>
                        <dt>Channel</dt>
                        <dd className="sm">{h.channel_status}</dd>
                      </dl>
                      <div className="eyebrow">Docks checked</div>
                      <ul className="checked">
                        {(r.docks_checked || []).map((d: any) => (
                          <li key={d.dock_code}>{d.detail}</li>
                        ))}
                      </ul>
                      {r.nearest_out_of_policy && (
                        <>
                          <div className="eyebrow">Nearest out-of-policy option</div>
                          <p className="sm">
                            {r.nearest_out_of_policy.notes?.join("; ") ||
                              `${r.nearest_out_of_policy.dock_code} ${hhmm(r.nearest_out_of_policy.start)} (${r.nearest_out_of_policy.why_out_of_policy})`}
                          </p>
                        </>
                      )}
                      {r.data_flags?.length > 0 && (
                        <p className="alert sm">{r.data_flags.map((f: any) => f.detail).join("; ")}</p>
                      )}
                    </div>
                  )}
                  <div className="btn-row">
                    <button className="btn sm" disabled={busy === h.handoff_id} onClick={() => closeHof(h)}>
                      Mark resolved
                    </button>
                  </div>
                </article>
              );
            })}
          </section>
        </aside>
      </main>
    </div>
  );
}
