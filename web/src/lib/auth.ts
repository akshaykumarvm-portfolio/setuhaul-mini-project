// Sign-in. "cognito" = Amazon Cognito user pool via its public InitiateAuth API (no SDK);
// "local" = persona picker for a booking service running with AUTH_MODE=off.

export const AUTH_MODE = (import.meta.env.VITE_AUTH_MODE as string) || "local";
const POOL_ID = (import.meta.env.VITE_COGNITO_USER_POOL_ID as string) || "";
const CLIENT_ID = (import.meta.env.VITE_COGNITO_CLIENT_ID as string) || "";
const REGION = POOL_ID.split("_")[0] || "us-east-1";
const KEY = "setuhaul.session";

export type Role = "driver" | "coordinator" | "planner";
export interface Session {
  role: Role;
  username: string;
  driverId?: string;
  facilityId?: string;
  idToken?: string; // Cognito ID token, sent as Bearer
  refreshToken?: string;
  expiresAt?: number; // epoch ms
}

function decode(jwt: string): Record<string, any> {
  const b64 = jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64.padEnd(Math.ceil(b64.length / 4) * 4, "="));
  return JSON.parse(new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0))));
}

function save(s: Session | null) {
  try {
    if (s) localStorage.setItem(KEY, JSON.stringify(s));
    else localStorage.removeItem(KEY);
  } catch {
    /* private mode: the session lasts for this page only */
  }
}

function load(): Session | null {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

async function cognito(target: string, body: object): Promise<any> {
  const r = await fetch(`https://cognito-idp.${REGION}.amazonaws.com/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}`,
    },
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.message || data.__type || "Sign-in failed");
  return data;
}

function fromTokens(idToken: string, refreshToken: string | undefined, expiresIn: number): Session {
  const p = decode(idToken);
  const groups: string[] = p["cognito:groups"] || [];
  const role: Role = groups.includes("planner") ? "planner" : groups.includes("coordinator") ? "coordinator" : "driver";
  return {
    role,
    username: p["cognito:username"],
    driverId: p["custom:driver_id"],
    facilityId: p["custom:facility_id"],
    idToken,
    refreshToken,
    expiresAt: Date.now() + (expiresIn - 60) * 1000,
  };
}

export async function signIn(username: string, password: string): Promise<Session> {
  const r = await cognito("InitiateAuth", {
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: CLIENT_ID,
    AuthParameters: { USERNAME: username, PASSWORD: password },
  });
  if (r.ChallengeName) throw new Error(`This account needs ${r.ChallengeName}. Ask the admin to reset it.`);
  const a = r.AuthenticationResult;
  const s = fromTokens(a.IdToken, a.RefreshToken, a.ExpiresIn);
  save(s);
  return s;
}

/** Current session, refreshing the ID token when it is about to expire. */
export async function currentSession(): Promise<Session | null> {
  const s = load();
  if (!s || AUTH_MODE !== "cognito") return s;
  if (s.expiresAt && Date.now() < s.expiresAt) return s;
  if (!s.refreshToken) return null;
  try {
    const r = await cognito("InitiateAuth", {
      AuthFlow: "REFRESH_TOKEN_AUTH",
      ClientId: CLIENT_ID,
      AuthParameters: { REFRESH_TOKEN: s.refreshToken },
    });
    const a = r.AuthenticationResult;
    const fresh = fromTokens(a.IdToken, s.refreshToken, a.ExpiresIn);
    save(fresh);
    return fresh;
  } catch {
    save(null);
    return null;
  }
}

export function localSignIn(s: Session) {
  save(s);
  return s;
}

export function signOut() {
  save(null);
}
