// Booking API client. Every call carries the Cognito ID token (cognito mode) or X-Driver-Id (local dev).
import { AUTH_MODE, currentSession, Session } from "./auth";

export const API_BASE = (import.meta.env.VITE_API_BASE as string) || "/api";

export class ApiError extends Error {
  constructor(public status: number, public body: any) {
    super(body?.message || body?.detail || `HTTP ${status}`);
  }
}

async function headers(): Promise<Record<string, string>> {
  const s = await currentSession();
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (AUTH_MODE === "cognito" && s?.idToken) h.Authorization = `Bearer ${s.idToken}`;
  if (AUTH_MODE !== "cognito" && s?.driverId) h["X-Driver-Id"] = s.driverId;
  return h;
}

export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, { ...init, headers: { ...(await headers()), ...(init.headers || {}) } });
  const body = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok && !(body && typeof body === "object" && "ok" in body)) throw new ApiError(r.status, body);
  return body as T;
}

export const post = <T = any>(path: string, data?: unknown) =>
  api<T>(path, { method: "POST", body: data === undefined ? undefined : JSON.stringify(data) });

/** Server-sent events URL (EventSource can't set headers, so the token goes in the query). */
export async function eventsUrl(facilityId: string, s: Session | null): Promise<string> {
  const q = AUTH_MODE === "cognito" && s?.idToken ? `?token=${encodeURIComponent(s.idToken)}` : "";
  return `${API_BASE}/events/${facilityId}${q}`;
}

// ---------- shapes returned by the booking service ----------
export interface Appt {
  appointment_id: string;
  status: string;
  dock_code: string;
  start: string;
  end: string;
  slot_ids: string[];
  expires_at?: string;
}
export interface Shipment {
  shipment_id: string;
  order_reference: string;
  facility_id: string;
  status: string;
  live: boolean;
  priority: string;
  dock_type: string;
  load_weight_kg: number;
  expected_unload_min: number;
  eta: { eta_lo: string; eta_hi: string; confidence: string; source: string };
  current_appointment: Appt | null;
  checkin: { gate_in_ts: string | null; queue_state: string | null } | null;
  flags: { code: string; detail: string }[];
}
export interface Status {
  ok: boolean;
  shipment_id: string;
  shipment_status: string;
  stage: "NONE" | "HELD" | "PENDING" | "CONFIRMED";
  sim_now: string;
  current_appointment: Appt | null;
  pending_request: Appt | null;
  hold: { hold_id: string; expires_at: string } | null;
  last_closed_request: { appointment_id: string; status: string; reason: string } | null;
  open_handoff: { handoff_id: string; reason_code: string; routed_to: string } | null;
  eta: { eta_lo: string; eta_hi: string; confidence: string; source: string };
}
