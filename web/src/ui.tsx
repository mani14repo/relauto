import type { Result, Risk } from "./api";

export function ResultBadge({ r }: { r: Result | null | undefined }) {
  const cls = !r ? "r-none" : r === "Insufficient Evidence" ? "r-Insufficient" : r === "Not Applicable" ? "r-NA" : `r-${r}`;
  return <span className={`badge ${cls}`}>{r ?? "Not evaluated"}</span>;
}

export function Severity({ s }: { s: Risk | null | undefined }) {
  return s ? <span className={`s-${s}`} style={{ fontWeight: 600 }}>{s}</span> : <span className="muted">—</span>;
}

export function ErrorBox({ error }: { error: string | null }) {
  return error ? <div className="error" role="alert">{error}</div> : null;
}

export function Bar({ value }: { value: number | null }) {
  return <div className="bar" aria-label={`${value ?? 0}%`}><span style={{ width: `${Math.max(0, Math.min(100, value ?? 0))}%` }} /></div>;
}

export const RATING_COLOR: Record<string, string> = {
  "Not Ready": "var(--gap)", "At Risk": "var(--high)", Conditional: "var(--ie)", Ready: "var(--met)",
};
