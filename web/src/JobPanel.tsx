import { useEffect, useRef, useState } from "react";

export type JobLine = { t: string; level: "info" | "warn" | "error"; msg: string };
export type Job = {
  state: "idle" | "queued" | "running" | "done" | "failed" | "cancelled";
  phase?: string; step?: number; total?: number; current?: string;
  started_at?: string; updated_at?: string; finished_at?: string | null; eta_s?: number | null;
  log?: JobLine[]; warnings?: number; error?: string | null;
  summary?: { rating: string; index: number; llm?: { calls: number; verified: number; rejected: number; no_evidence: number; errors: number } | null } | null;
  job?: { llm?: boolean };
  seconds_since_update?: number; stale_after_s?: number; stale?: boolean; cancel_requested?: boolean;
};

export const isActive = (j: Job | null | undefined) => !!j && (j.state === "queued" || j.state === "running");

function dur(s: number | null | undefined) {
  if (s == null || !isFinite(s)) return "—";
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return `${m}m ${String(r).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}
const since = (iso: string | undefined, now: number) => (iso ? (now - Date.parse(iso)) / 1000 : null);
const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function dismissedKey(id: string) { return `job.dismissed.${id}`; }
function getDismissed(id: string) { try { return window.sessionStorage.getItem(dismissedKey(id)); } catch { return null; } }
function setDismissed(id: string, v: string) { try { window.sessionStorage.setItem(dismissedKey(id), v); } catch { /* ignore */ } }

/** Live status of the automated pass: phase, progress, the criterion being evaluated, and an activity log. */
export default function JobPanel({ id, job, onCancel }: { id: string; job: Job | null; onCancel?: () => Promise<void> }) {
  const [cancelling, setCancelling] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [dismissed, setDis] = useState<string | null>(() => getDismissed(id));
  const logRef = useRef<HTMLDivElement>(null);
  const active = isActive(job);

  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [active]);
  useEffect(() => { const el = logRef.current; if (el) el.scrollTop = el.scrollHeight; }, [job?.log?.length]);

  if (!job || job.state === "idle") return null;
  if (!active && job.finished_at && dismissed === job.finished_at) return null;

  const lines = job.log ?? [];
  const total = job.total ?? 0, step = job.step ?? 0;
  const pct = total ? Math.round((100 * step) / total) : null;
  const quiet = active ? Math.max(job.seconds_since_update ?? 0, since(job.updated_at, now) ?? 0) : 0;
  const stale = active && (job.stale || quiet > (job.stale_after_s ?? 420));
  const elapsed = active ? since(job.started_at, now) : job.finished_at && job.started_at
    ? (Date.parse(job.finished_at) - Date.parse(job.started_at)) / 1000 : null;
  const llm = job.summary?.llm;
  const warnLines = lines.filter((l) => l.level !== "info");

  let tone = "info", title = "";
  if (job.state === "queued") title = "Queued";
  else if (job.state === "running") title = job.phase ? `Running · ${job.phase}` : "Running";
  else if (job.state === "done") { title = "Automated pass finished"; tone = job.warnings ? "warn" : "ok"; }
  else if (job.state === "cancelled") { title = "Automated pass cancelled"; tone = "warn"; }
  else { title = "Automated pass failed"; tone = "bad"; }
  const stopping = active && (job.cancel_requested || cancelling);
  if (stopping && !stale) title = "Cancelling…";
  if (stale) { title = `No progress for ${dur(quiet)}`; tone = "bad"; }

  return (
    <section className={`card job job-${tone}`} role="status" aria-live="polite" aria-label="Automated pass status">
      <div className="row" style={{ gap: 10 }}>
        {active && !stale && <span className="spinner" aria-hidden="true" />}
        <strong>{title}</strong>
        {job.job?.llm !== undefined && <span className="pill">{job.job.llm ? "with local LLM" : "rules only"}</span>}
        <span style={{ flex: 1 }} />
        <span className="muted small">
          {elapsed != null && <>{active ? "Elapsed" : "Took"} {dur(elapsed)}</>}
          {active && job.updated_at && <> · last update {dur(since(job.updated_at, now))} ago</>}
          {!active && job.finished_at && <> · {clock(job.finished_at)}</>}
        </span>
        {!active && job.finished_at && (
          <button className="link" onClick={() => { setDismissed(id, job.finished_at!); setDis(job.finished_at!); }}>Dismiss</button>
        )}
        {active && onCancel && (
          <button disabled={stopping && !stale} onClick={async () => {
            if (!window.confirm("Cancel this automated pass? Nothing from this run will be saved; the assessment keeps its previous results.")) return;
            setCancelling(true);
            try { await onCancel(); } finally { setCancelling(false); }
          }}>{stopping && !stale ? "Cancelling…" : "Cancel run"}</button>
        )}
      </div>

      {active && total > 0 && (
        <div style={{ marginTop: 10 }}>
          <div className="bar job-bar" aria-label={`${step} of ${total}`}><span style={{ width: `${pct}%` }} /></div>
          <div className="row small muted" style={{ marginTop: 4 }}>
            <span>{step} of {total} {job.phase === "AI-assisted criteria" ? "criteria" : "steps"} done ({pct}%)</span>
            <span style={{ flex: 1 }} />
            {job.eta_s != null && <span>about {dur(job.eta_s)} left</span>}
          </div>
        </div>
      )}
      {active && job.current && (
        <p className="small" style={{ margin: "8px 0 0" }}>
          Now: <span className="mono">{job.current}</span>
          {/^[A-Z]+-AT-\d+-C\d+/.test(job.current) && <span className="muted"> · waiting for the model for {dur(since(job.updated_at, now))}</span>}
        </p>
      )}
      {job.state === "queued" && !stale && !stopping && <p className="small muted" style={{ margin: "8px 0 0" }}>Waiting for a worker to pick up the job…</p>}
      {stopping && !stale && <p className="small muted" style={{ margin: "8px 0 0" }}>Stopping at the next step; a model call in progress is abandoned within a second or two.</p>}
      {job.state === "cancelled" && <p className="small" style={{ margin: "8px 0 0" }}>Nothing from this run was saved; the assessment keeps its previous results.</p>}
      {stale && (
        <p className="small" style={{ margin: "8px 0 0" }}>
          The run has not reported anything for {dur(quiet)}, longer than one model call is allowed to take. The server or worker
          may have stopped (for example after a restart). Check the server console, then run the assessment again from the Evidence tab.
        </p>
      )}
      {job.state === "failed" && job.error && <p className="small" style={{ margin: "8px 0 0" }}><strong>Error:</strong> <span className="mono">{job.error}</span></p>}
      {job.state === "done" && job.summary && (
        <p className="small" style={{ margin: "8px 0 0" }}>
          Rated <strong>{job.summary.rating}</strong> (index {job.summary.index}).
          {llm && <> Local LLM: {llm.verified} verified, {llm.rejected} rejected by verification, {llm.no_evidence} without evidence, {llm.errors} errors.</>}
          {!!warnLines.length && <> {warnLines.length} warning{warnLines.length > 1 ? "s" : ""}: see the log.</>}
        </p>
      )}

      {!!lines.length && (
        <details className="job-log-wrap" open={active || job.state === "failed" || job.state === "cancelled" || !!warnLines.length}>
          <summary className="small">Activity log ({lines.length}{job.warnings ? `, ${job.warnings} warnings` : ""})</summary>
          <div className="job-log mono small" ref={logRef}>
            {lines.map((l, i) => (
              <div key={i} className={`lvl-${l.level}`}><span className="muted">{clock(l.t)}</span> {l.level !== "info" && <b>{l.level.toUpperCase()} </b>}{l.msg}</div>
            ))}
          </div>
        </details>
      )}
    </section>
  );
}
