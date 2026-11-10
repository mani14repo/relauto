import { useEffect, useRef } from "react";
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

/** A small modal "are you sure?" box: Escape or Cancel closes it, the danger button confirms. */
export function ConfirmDialog({ title, body, confirmLabel, busy, onConfirm, onCancel }: {
  title: string; body: string; confirmLabel: string; busy?: boolean; onConfirm: () => void; onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const d = ref.current; if (d && !d.open) d.showModal(); }, []);
  return (
    <dialog ref={ref} className="confirm" onCancel={(e) => { e.preventDefault(); if (!busy) onCancel(); }}
      onClick={(e) => { if (e.target === ref.current && !busy) onCancel(); }}>
      <h2>{title}</h2>
      <p>{body}</p>
      <div className="row" style={{ gap: 8, justifyContent: "flex-end" }}>
        <button onClick={onCancel} disabled={busy} autoFocus>Cancel</button>
        <button className="danger" onClick={onConfirm} disabled={busy}>{busy ? "Deleting…" : confirmLabel}</button>
      </div>
    </dialog>
  );
}

export function TrashIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 6h18" /><path d="M8 6V4h8v2" /><path d="M19 6l-1 14H6L5 6" /><path d="M10 11v6" /><path d="M14 11v6" />
    </svg>
  );
}

export const deleteMessage = (name: string) =>
  `This removes "${name}" with its evidence files, results, findings, sign-offs and audit trail. It cannot be undone.`;
