// All times are shown in the warehouse's local time (Asia/Kolkata), as the booking service stores them.
export const hhmm = (ts?: string | null) => (ts ? ts.slice(11, 16) : "--:--");

export const minutesOf = (ts: string) => {
  const [h, m] = ts.slice(11, 16).split(":").map(Number);
  return h * 60 + m;
};

/** Seconds between two ISO timestamps with offsets (b - a). */
export const secondsBetween = (a: string, b: string) => (Date.parse(b) - Date.parse(a)) / 1000;

export function countdown(fromTs: string, toTs: string): string {
  const s = Math.max(0, Math.round(secondsBetween(fromTs, toTs)));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

export const label = (code: string) => code.toLowerCase().replace(/_/g, " ");
