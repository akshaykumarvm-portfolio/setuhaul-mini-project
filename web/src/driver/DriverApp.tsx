import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, post, Shipment, Status } from "../lib/api";
import { Session } from "../lib/auth";
import { countdown, hhmm, label } from "../lib/time";
import Brand from "../components/Brand";

interface Msg {
  who: "driver" | "agent" | "system";
  text: string;
  note?: string;
}

const STAGES = ["Reported", "Options", "Held", "Requested", "Confirmed"] as const;
const QUICK = ["I'm running late", "Show me slot options", "Yes, request it", "What's my status?"];

function threadKey(driverId: string) {
  return `setuhaul.thread.${driverId}`;
}
function loadThread(driverId: string): string {
  try {
    const t = localStorage.getItem(threadKey(driverId));
    if (t) return t;
    const fresh = `CHAT-${driverId}-${Math.random().toString(36).slice(2, 10)}`;
    localStorage.setItem(threadKey(driverId), fresh);
    return fresh;
  } catch {
    return `CHAT-${driverId}-${Math.random().toString(36).slice(2, 10)}`;
  }
}

export default function DriverApp({ session, onSignOut }: { session: Session; onSignOut: () => void }) {
  const driverId = session.driverId!;
  const [threadId, setThreadId] = useState(() => loadThread(driverId));
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [me, setMe] = useState<{ driver: any; shipments: Shipment[]; sim_now: string } | null>(null);
  const [linked, setLinked] = useState<string | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [quoted, setQuoted] = useState(false);
  const [confirmedHere, setConfirmedHere] = useState(false);
  const prevStage = useRef<string | null>(null);
  const pendingId = useRef<string | null>(null); // the request this conversation is waiting on
  const endRef = useRef<HTMLDivElement>(null);

  const live = useMemo(() => (me?.shipments || []).filter((s) => s.live), [me]);
  const focus = linked || (live.length === 1 ? live[0].shipment_id : null);
  const focusShip = (me?.shipments || []).find((s) => s.shipment_id === focus) || null;

  // load identity, loads and the stored transcript for this thread
  useEffect(() => {
    api("/me").then(setMe).catch(() => setMe(null));
    api<{ messages: { sender_type: string; message_text: string }[] }>(`/threads/${threadId}/messages`)
      .then((r) =>
        setMsgs(
          (r.messages || []).map((m) => ({ who: m.sender_type === "DRIVER" ? "driver" : "agent", text: m.message_text })),
        ),
      )
      .catch(() => setMsgs([]));
  }, [threadId]);

  // booking state is the source of truth for the trip card; poll so a coordinator's decision shows up live
  const refreshStatus = useCallback(async () => {
    if (!focus) return;
    try {
      const s = await api<Status>(`/shipments/${focus}/status`);
      if (!s.ok) return;
      if (prevStage.current === "PENDING" && s.stage === "CONFIRMED" && s.current_appointment) {
        setConfirmedHere(true);
        setMsgs((m) => [
          ...m,
          {
            who: "system",
            text: `Warehouse confirmed: ${s.current_appointment!.dock_code}, ${hhmm(s.current_appointment!.start)}–${hhmm(
              s.current_appointment!.end,
            )}.`,
          },
        ]);
      }
      // only report a closed request if it is THE request we were waiting on (never an older one)
      if (
        prevStage.current === "PENDING" &&
        s.stage !== "PENDING" &&
        s.last_closed_request &&
        s.last_closed_request.appointment_id === pendingId.current
      ) {
        setMsgs((m) => [
          ...m,
          { who: "system", text: `Request ${label(s.last_closed_request!.status)}: ${s.last_closed_request!.reason}` },
        ]);
      }
      pendingId.current = s.pending_request?.appointment_id ?? null;
      prevStage.current = s.stage;
      setStatus(s);
    } catch {
      /* transient: next poll retries */
    }
  }, [focus]);

  useEffect(() => {
    refreshStatus();
    const t = setInterval(refreshStatus, 5000);
    return () => clearInterval(t);
  }, [refreshStatus]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" }); // newer browsers return a Promise: never return it
  }, [msgs, sending]);

  const send = async (text: string) => {
    const t = text.trim();
    if (!t || sending) return;
    setInput("");
    setMsgs((m) => [...m, { who: "driver", text: t }]);
    setSending(true);
    try {
      const r = await post("/chat", { text: t, thread_id: threadId, message_id: crypto.randomUUID() });
      setMsgs((m) => [...m, { who: "agent", text: r.reply || "…", note: r.duplicate ? "already received" : undefined }]);
      if (r.state?.shipment_id) setLinked(r.state.shipment_id);
      if (r.state?.quote_id) setQuoted(true);
      setTimeout(refreshStatus, 300);
    } catch (e: any) {
      setMsgs((m) => [...m, { who: "system", text: `Couldn't reach the assistant (${e.message}). Your slot is unchanged.` }]);
    } finally {
      setSending(false);
    }
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    send(input);
  };

  const newConversation = () => {
    const fresh = `CHAT-${driverId}-${Math.random().toString(36).slice(2, 10)}`;
    try {
      localStorage.setItem(threadKey(driverId), fresh);
    } catch {
      /* ignore */
    }
    setLinked(null);
    setQuoted(false);
    setConfirmedHere(false);
    setStatus(null);
    prevStage.current = null;
    pendingId.current = null;
    setThreadId(fresh);
  };

  // Progress through THIS conversation; "Confirmed" lights up only when the warehouse confirms our request.
  const stageIdx = confirmedHere
    ? 4
    : status?.stage === "PENDING"
      ? 3
      : status?.stage === "HELD"
        ? 2
        : quoted
          ? 1
          : msgs.length
            ? 0
            : -1;

  return (
    <div className="driver-shell">
      <header className="topbar">
        <Brand compact />
        <div className="topbar-right">
          <span className="who">
            <span className="mono">{driverId}</span>
            {me?.driver?.driver_name ? ` · ${me.driver.driver_name}` : ""}
          </span>
          <button className="btn ghost sm" onClick={newConversation} title="Start a new conversation">
            New chat
          </button>
          <button className="btn ghost sm" onClick={onSignOut}>
            Sign out
          </button>
        </div>
      </header>

      <main className="driver-main">
        <section className="trip">
          {live.length === 0 && me && <div className="trip-empty">No live loads assigned to you today.</div>}
          {live.length > 1 && !linked && (
            <div className="trip-multi">
              You have {live.length} live loads today. Tell the assistant which one you mean.
              <div className="chips">
                {live.map((s) => (
                  <button key={s.shipment_id} className="chip" onClick={() => send(`It's about ${s.shipment_id}`)}>
                    {s.shipment_id} → {s.facility_id === "FAC-JAI-01" ? "Jaipur" : "Gurugram"}
                  </button>
                ))}
              </div>
            </div>
          )}
          {focusShip && (
            <div className="trip-card">
              <div className="trip-row">
                <div>
                  <div className="eyebrow">Load</div>
                  <div className="mono big">{focusShip.shipment_id}</div>
                  <div className="muted">{focusShip.order_reference}</div>
                </div>
                <div className="right">
                  <span className={`badge pr-${focusShip.priority.toLowerCase()}`}>{focusShip.priority}</span>
                  <div className="muted sm">
                    {focusShip.facility_id === "FAC-JAI-01" ? "Jaipur DC" : "Gurugram DC"} · {focusShip.dock_type.toLowerCase()} ·{" "}
                    {(focusShip.load_weight_kg / 1000).toFixed(1)} t
                  </div>
                </div>
              </div>
              <div className="trip-grid">
                <div>
                  <div className="eyebrow">ETA on record</div>
                  <div className="val">
                    {hhmm((status?.eta || focusShip.eta).eta_lo) === hhmm((status?.eta || focusShip.eta).eta_hi)
                      ? hhmm((status?.eta || focusShip.eta).eta_hi)
                      : `${hhmm((status?.eta || focusShip.eta).eta_lo)}–${hhmm((status?.eta || focusShip.eta).eta_hi)}`}
                    <span className={`conf conf-${(status?.eta || focusShip.eta).confidence.toLowerCase()}`}>
                      {(status?.eta || focusShip.eta).confidence}
                    </span>
                  </div>
                </div>
                <div>
                  <div className="eyebrow">Your slot</div>
                  <div className="val">
                    {status?.current_appointment
                      ? `${status.current_appointment.dock_code} · ${hhmm(status.current_appointment.start)}–${hhmm(
                          status.current_appointment.end,
                        )}`
                      : focusShip.current_appointment
                        ? `${focusShip.current_appointment.dock_code} · ${hhmm(focusShip.current_appointment.start)}`
                        : "None"}
                  </div>
                </div>
              </div>
              {status?.hold && (
                <div className="trip-flag hold">
                  Holding a slot for you · expires in <b>{countdown(status.sim_now, status.hold.expires_at)}</b> · not booked yet
                </div>
              )}
              {status?.pending_request && (
                <div className="trip-flag pending">
                  Requested {status.pending_request.dock_code} {hhmm(status.pending_request.start)} · waiting for the warehouse
                  {status.pending_request.expires_at && <> · lapses at {hhmm(status.pending_request.expires_at)}</>}
                </div>
              )}
              {status?.open_handoff && (
                <div className="trip-flag handoff">A coordinator has your case and will contact you.</div>
              )}
              <ol className="stepper" aria-label="Progress">
                {STAGES.map((s, i) => (
                  <li key={s} className={i < stageIdx ? "done" : i === stageIdx ? "now" : ""}>
                    <span />
                    {s}
                  </li>
                ))}
              </ol>
            </div>
          )}
        </section>

        <section className="chat">
          <div className="chat-log" aria-live="polite">
            {msgs.length === 0 && (
              <div className="chat-hello">
                <b>Running late or early?</b> Tell me your new arrival time and I'll check what the dock can offer. Nothing is
                booked until the warehouse confirms.
              </div>
            )}
            {msgs.map((m, i) => (
              <div key={i} className={`bubble ${m.who}`}>
                {m.text}
                {m.note && <span className="bubble-note">{m.note}</span>}
              </div>
            ))}
            {sending && <div className="bubble agent typing">Checking the dock schedule…</div>}
            <div ref={endRef} />
          </div>
          <div className="chips quick">
            {QUICK.map((q) => (
              <button key={q} className="chip" onClick={() => send(q)} disabled={sending}>
                {q}
              </button>
            ))}
          </div>
          <form className="composer" onSubmit={onSubmit}>
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="e.g. Traffic near Shahpura, reaching the gate at 11:20"
              aria-label="Message"
              disabled={sending}
            />
            <button className="btn primary" disabled={sending || !input.trim()}>
              Send
            </button>
          </form>
        </section>
      </main>
    </div>
  );
}
