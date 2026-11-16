import { hhmm, label, minutesOf } from "../lib/time";

export interface SlotRow {
  slot_id: string;
  dock_code: string;
  dock_type: string;
  slot_start_ts: string;
  slot_end_ts: string;
  slot_status: string;
  block_reason: string | null;
  appointment_id: string | null;
  shipment_id: string | null;
  appointment_status: string | null;
  priority_code: string | null;
  expected_unload_min: number | null;
  hold_id: string | null;
  held_by: string | null;
  expires_at: string | null;
}
export interface DockEvent {
  dock_event_id: string;
  dock_code: string;
  event_type: string;
  event_start_ts: string;
  event_end_ts: string | null;
  reason: string;
}
export interface Dock {
  dock_code: string;
  dock_type: string;
  max_vehicle_weight_kg: number;
}

const START = 8 * 60;
const END = 22 * 60;
const pct = (m: number) => `${((Math.min(Math.max(m, START), END) - START) / (END - START)) * 100}%`;
const width = (a: number, b: number) => `${((Math.min(b, END) - Math.max(a, START)) / (END - START)) * 100}%`;

function slotState(s: SlotRow): string {
  if (s.slot_status !== "OPEN") return "blocked";
  if (s.appointment_status === "IN_PROGRESS") return "inprogress";
  if (s.appointment_status === "CONFIRMED") return "confirmed";
  if (s.appointment_status === "PENDING_CONFIRMATION") return "pending";
  if (s.hold_id) return "held";
  return "free";
}

export default function Timeline({
  docks,
  slots,
  events,
  simNow,
  highlight,
}: {
  docks: Dock[];
  slots: SlotRow[];
  events: DockEvent[];
  simNow: string;
  highlight?: Set<string>;
}) {
  const hours = Array.from({ length: (END - START) / 60 + 1 }, (_, i) => START / 60 + i);
  const now = minutesOf(simNow);
  return (
    <div className="timeline" role="table" aria-label="Dock timeline">
      <div className="tl-head" role="row">
        <div className="tl-dock-col" />
        <div className="tl-scale">
          {hours.map((h) => (
            <span key={h} style={{ left: pct(h * 60) }}>
              {String(h).padStart(2, "0")}
            </span>
          ))}
        </div>
      </div>
      {docks.map((d) => (
        <div className="tl-row" role="row" key={d.dock_code}>
          <div className="tl-dock-col">
            <span className="mono">{d.dock_code}</span>
            <small>
              {d.dock_type.toLowerCase()} · {d.max_vehicle_weight_kg / 1000}t
            </small>
          </div>
          <div className="tl-lane">
            {hours.map((h) => (
              <i key={h} className="tl-grid" style={{ left: pct(h * 60) }} />
            ))}
            {slots
              .filter((s) => s.dock_code === d.dock_code)
              .map((s) => {
                const a = minutesOf(s.slot_start_ts);
                const b = minutesOf(s.slot_end_ts);
                const st = slotState(s);
                const who = s.shipment_id || s.held_by;
                const tip = [
                  `${s.slot_id}  ${hhmm(s.slot_start_ts)}–${hhmm(s.slot_end_ts)}`,
                  st === "blocked" ? `Blocked: ${s.block_reason || label(s.slot_status)}` : label(st),
                  who ? `${who}${s.priority_code ? ` · ${s.priority_code}` : ""}` : "",
                  s.appointment_id ? s.appointment_id : "",
                  s.expires_at ? `hold expires ${hhmm(s.expires_at)}` : "",
                ]
                  .filter(Boolean)
                  .join("\n");
                return (
                  <div
                    key={s.slot_id}
                    className={`tl-slot ${st} ${s.appointment_id && highlight?.has(s.appointment_id) ? "flash" : ""}`}
                    style={{ left: pct(a), width: width(a, b) }}
                    title={tip}
                  >
                    {who && <span className="mono">{who.replace("SHP", "")}</span>}
                    {s.priority_code === "CRITICAL" && <b className="crit">!</b>}
                  </div>
                );
              })}
            {events
              .filter((e) => e.dock_code === d.dock_code && e.event_type !== "REOPENED")
              .map((e) => {
                const a = minutesOf(e.event_start_ts);
                const b = e.event_end_ts ? minutesOf(e.event_end_ts) : END;
                return (
                  <div
                    key={e.dock_event_id}
                    className={`tl-event ${e.event_type.toLowerCase()}`}
                    style={{ left: pct(a), width: width(a, b) }}
                    title={`${label(e.event_type)} ${hhmm(e.event_start_ts)}–${e.event_end_ts ? hhmm(e.event_end_ts) : "?"}\n${e.reason}`}
                  >
                    <span>{label(e.event_type)}</span>
                  </div>
                );
              })}
            {now >= START && now <= END && <div className="tl-now" style={{ left: pct(now) }} />}
          </div>
        </div>
      ))}
      <div className="tl-legend">
        {["free", "held", "pending", "confirmed", "inprogress", "blocked"].map((k) => (
          <span key={k}>
            <i className={`tl-slot ${k}`} />
            {k === "inprogress" ? "unloading" : k}
          </span>
        ))}
        <span>
          <i className="tl-event breakdown" />
          dock event
        </span>
        <span>
          <i className="tl-now-key" />
          now
        </span>
      </div>
    </div>
  );
}
