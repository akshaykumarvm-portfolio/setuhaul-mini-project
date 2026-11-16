import { useState } from "react";
import { api } from "../lib/api";

type Row = {
  shipment_id: string;
  priority: string;
  eta: string;
  earliest: string;
  unload_min: number;
  promised: string | null;
  dock: string | null;
  start: string | null;
  end: string | null;
  late_min: number | null;
  changed: boolean;
  needs_approval: boolean;
  frozen: boolean;
  flags: string[];
};
type Plan = {
  label: string;
  metrics: Record<string, number>;
  violations: string[];
  rows: Row[];
  status?: string;
  solve_s?: number;
  proven_optimal?: boolean;
};
type Proposal = {
  sim_now: string;
  trucks: number;
  docks: Record<string, string>;
  fixed: Record<string, { start: string; end: string; why: string }[]>;
  plans: { current: Plan; dispatch_rule: Plan; cpsat: Plan };
  recommendation: "cpsat" | "dispatch_rule";
  note: string;
};

const METRICS: [string, string, boolean][] = [
  ["weighted_lateness", "Priority-weighted lateness", true],
  ["late_trucks", "Trucks later than due", true],
  ["waiting_min", "Waiting after earliest start (min)", true],
  ["promises_changed", "Promises changed", true],
  ["unscheduled", "No feasible slot (hand-off)", true],
  ["needs_approval", "Needs approval", true],
  ["utilisation_pct", "Dock utilisation %", false],
];
const toMin = (t: string | null) => (t ? Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5)) : 0);
const START = 8 * 60;
const END = 22 * 60;
const pct = (m: number) => `${((Math.min(Math.max(m, START), END) - START) / (END - START)) * 100}%`;
const span = (a: number, b: number) => `${((Math.min(b, END) - Math.max(a, START)) / (END - START)) * 100}%`;

function Gantt({ p, plan }: { p: Proposal; plan: Plan }) {
  const docks = Object.values(p.docks).sort();
  const now = toMin(p.sim_now);
  return (
    <div className="timeline plan-gantt">
      {docks.map((code) => (
        <div className="tl-row" key={code}>
          <div className="tl-dock-col">
            <span className="mono">{code}</span>
          </div>
          <div className="tl-lane">
            {(p.fixed[code] || []).map((f, i) => (
              <div key={i} className="tl-slot blocked" style={{ left: pct(toMin(f.start)), width: span(toMin(f.start), toMin(f.end)) }} title={f.why} />
            ))}
            {plan.rows
              .filter((r) => r.dock === code)
              .map((r) => (
                <div
                  key={r.shipment_id}
                  className={`plan-bar pr-${r.priority.toLowerCase()} ${r.changed ? "moved" : ""}`}
                  style={{ left: pct(toMin(r.start)), width: span(toMin(r.start), toMin(r.end)) }}
                  title={`${r.shipment_id} ${r.priority} ${r.start}–${r.end}${r.promised ? `\npromised ${r.promised}` : ""}${r.late_min ? `\n${r.late_min} min late vs due` : ""}`}
                >
                  <span className="mono">{r.shipment_id.replace("SHP", "")}</span>
                </div>
              ))}
            {now >= START && now <= END && <div className="tl-now" style={{ left: pct(now) }} />}
          </div>
        </div>
      ))}
    </div>
  );
}

export default function PlanPanel({ facilityId }: { facilityId: string }) {
  const [p, setP] = useState<Proposal | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const run = async () => {
    setBusy(true);
    setErr(null);
    try {
      setP(await api<Proposal>(`/facilities/${facilityId}/proposal?time_limit=5`));
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  const rec = p ? p.plans[p.recommendation] : null;
  const keys = ["current", "dispatch_rule", "cpsat"] as const;
  return (
    <section className="panel plan-panel">
      <div className="panel-head">
        <h2>Facility re-plan (proposal only)</h2>
        <button className="btn primary sm" onClick={run} disabled={busy}>
          {busy ? "Solving…" : p ? "Re-run" : "Propose schedule"}
        </button>
      </div>
      {!p && !busy && (
        <p className="muted sm">
          Re-sequences every truck still to unload: current promises vs a dispatch rule (priority, then earliest) vs CP-SAT
          (OR-Tools, 5 s). Nothing is booked; any change still goes through hold → request → confirm.
        </p>
      )}
      {err && <div className="alert">{err}</div>}
      {p && rec && (
        <>
          <div className="plan-meta">
            <span>
              {p.trucks} trucks · at {p.sim_now}
            </span>
            <span>
              CP-SAT {p.plans.cpsat.status?.toLowerCase()} in {p.plans.cpsat.solve_s}s
              {p.plans.cpsat.proven_optimal ? " (proven optimal)" : ""}
            </span>
            <span className="rec">Recommended: {rec.label}</span>
          </div>
          <table className="cmp">
            <thead>
              <tr>
                <th />
                {keys.map((k) => (
                  <th key={k} className={k === p.recommendation ? "is-rec" : ""}>
                    {p.plans[k].label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {METRICS.map(([key, label, lowerBetter]) => {
                const vals = keys.slice(1).map((k) => p.plans[k].metrics[key]);
                const best = lowerBetter ? Math.min(...vals) : Math.max(...vals);
                return (
                  <tr key={key}>
                    <td>{label}</td>
                    {keys.map((k) => (
                      <td
                        key={k}
                        className={`mono ${k !== "current" && p.plans[k].metrics[key] === best && vals[0] !== vals[1] ? "best" : ""} ${
                          k === p.recommendation ? "is-rec" : ""
                        }`}
                      >
                        {p.plans[k].metrics[key]}
                      </td>
                    ))}
                  </tr>
                );
              })}
              <tr>
                <td>Rule violations</td>
                {keys.map((k) => (
                  <td key={k} className={`mono ${p.plans[k].violations.length ? "bad" : ""} ${k === p.recommendation ? "is-rec" : ""}`}>
                    {p.plans[k].violations.length}
                  </td>
                ))}
              </tr>
            </tbody>
          </table>
          {p.plans.current.violations.length > 0 && (
            <details className="why">
              <summary>Why the current promises can't all hold ({p.plans.current.violations.length})</summary>
              <ul>
                {p.plans.current.violations.map((v, i) => (
                  <li key={i}>{v}</li>
                ))}
              </ul>
            </details>
          )}
          <div className="eyebrow" style={{ marginTop: 12 }}>
            Recommended plan · outlined = promise changed
          </div>
          <Gantt p={p} plan={rec} />
          <table className="trucks">
            <thead>
              <tr>
                <th>Truck</th>
                <th>Prio</th>
                <th>ETA</th>
                <th>Earliest</th>
                <th>Promised</th>
                <th>Dispatch rule</th>
                <th>CP-SAT</th>
              </tr>
            </thead>
            <tbody>
              {p.plans.cpsat.rows.map((r) => {
                const b = p.plans.dispatch_rule.rows.find((x) => x.shipment_id === r.shipment_id)!;
                const cell = (x: Row) => (x.dock ? `${x.dock} ${x.start}${x.needs_approval ? " ⚠" : ""}` : "hand-off");
                return (
                  <tr key={r.shipment_id} className={cell(r) !== cell(b) ? "diff" : ""}>
                    <td className="mono">
                      {r.shipment_id}
                      {r.flags.length > 0 && <span title={r.flags.join(", ")}> ⚑</span>}
                      {r.frozen && <span className="muted"> (frozen)</span>}
                    </td>
                    <td>
                      <span className={`badge pr-${r.priority.toLowerCase()}`}>{r.priority}</span>
                    </td>
                    <td className="mono">{r.eta}</td>
                    <td className="mono">{r.earliest}</td>
                    <td className="mono">{r.promised || "–"}</td>
                    <td className="mono">{cell(b)}</td>
                    <td className="mono">{cell(r)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="fine">{p.note} ⚑ contradictory data: a person reviews first. ⚠ needs coordinator approval.</p>
        </>
      )}
    </section>
  );
}
