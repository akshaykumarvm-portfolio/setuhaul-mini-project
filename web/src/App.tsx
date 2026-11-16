import { useEffect, useState } from "react";
import { currentSession, Session, signOut } from "./lib/auth";
import Login from "./components/Login";
import DriverApp from "./driver/DriverApp";
import OpsConsole from "./ops/OpsConsole";

export default function App() {
  const [session, setSession] = useState<Session | null | undefined>(undefined);

  useEffect(() => {
    currentSession().then(setSession);
  }, []);

  if (session === undefined) return <div className="boot">Loading…</div>;
  if (!session) return <Login onSignedIn={setSession} />;

  const logout = () => {
    signOut();
    setSession(null);
  };
  return session.role === "driver" ? (
    <DriverApp session={session} onSignOut={logout} />
  ) : (
    <OpsConsole session={session} onSignOut={logout} />
  );
}
