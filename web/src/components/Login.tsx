import { FormEvent, useState } from "react";
import { AUTH_MODE, localSignIn, Session, signIn } from "../lib/auth";
import Brand from "./Brand";

// Seeded drivers and the case each one exercises (local dev persona picker).
const PERSONAS: { id: string; name: string; hint: string }[] = [
  { id: "DRV006", name: "SHP1006 · HIGH", hint: "Late after Shahpura (THR001); also has a Gurugram load" },
  { id: "DRV013", name: "SHP1013 · NORMAL", hint: "“Late by one hour” — vague ETA (THR003)" },
  { id: "DRV004", name: "SHP1004 + SHP1020", hint: "Two live loads, unclear which (THR010)" },
  { id: "DRV014", name: "SHP1014 · CRITICAL", hint: "21 t load, overweight pending booking (A2)" },
  { id: "DRV012", name: "SHP1012 · NORMAL", hint: "Tyre repaired, moving again (THR002)" },
  { id: "DRV001", name: "SHP1017 · LOW", hint: "Ask-only, arrival 12:30–13:00 (THR011)" },
  { id: "DRV015", name: "SHP1015 · reefer", hint: "Set clock to 16:30: no feasible slot (THR005)" },
  { id: "DRV008", name: "SHP1019 · cancelled", hint: "Asks about a cancelled load (THR012)" },
];

export default function Login({ onSignedIn }: { onSignedIn: (s: Session) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await signIn(username.trim(), password));
    } catch (err: any) {
      setError(err?.message || "Sign-in failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <div className="login-card">
        <Brand />
        <p className="login-sub">Dock appointments for drivers and warehouse coordinators</p>
        {AUTH_MODE === "cognito" ? (
          <form onSubmit={submit} className="stack">
            <label>
              Username
              <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
            </label>
            <label>
              Password
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
            </label>
            {error && <div className="alert">{error}</div>}
            <button className="btn primary" disabled={busy}>
              {busy ? "Signing in…" : "Sign in"}
            </button>
          </form>
        ) : (
          <div className="stack">
            <div className="persona-head">Driver (phone chat)</div>
            <div className="persona-grid">
              {PERSONAS.map((p) => (
                <button
                  key={p.id}
                  className="persona"
                  onClick={() => onSignedIn(localSignIn({ role: "driver", username: p.id, driverId: p.id }))}
                >
                  <span className="mono">{p.id}</span>
                  <strong>{p.name}</strong>
                  <small>{p.hint}</small>
                </button>
              ))}
            </div>
            <div className="persona-head">Warehouse</div>
            <div className="persona-row">
              <button
                className="btn"
                onClick={() =>
                  onSignedIn(localSignIn({ role: "coordinator", username: "neha (Jaipur)", facilityId: "FAC-JAI-01" }))
                }
              >
                Coordinator · Jaipur DC
              </button>
              <button className="btn" onClick={() => onSignedIn(localSignIn({ role: "planner", username: "planner" }))}>
                Planner · all facilities
              </button>
            </div>
            <p className="fine">Local mode: the booking service runs with AUTH_MODE=off. In AWS this screen is Cognito sign-in.</p>
          </div>
        )}
      </div>
    </div>
  );
}
