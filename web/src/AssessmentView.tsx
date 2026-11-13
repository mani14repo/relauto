import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, getUser, type Assessment, type CmdbReadiness, type Candidate, type CriterionResult, type Finding, type ScoreCard, type Template } from "./api";
import JobPanel, { isActive, type Job } from "./JobPanel";
import { Bar, ConfirmDialog, deleteMessage, ErrorBox, RATING_COLOR, ResultBadge, Severity } from "./ui";

type Tab = "templates" | "evidence" | "criteria" | "findings" | "score" | "report";
const TABS: [Tab, string][] = [
  ["templates", "1 · Templates"], ["evidence", "2 · Evidence"], ["criteria", "3 · Criteria"],
  ["findings", "4 · Findings"], ["score", "5 · Score"], ["report", "6 · Report & sign-off"],
];

export default function AssessmentView({ id }: { id: string }) {
  const [a, setA] = useState<Assessment | null>(null);
  const [sc, setSc] = useState<ScoreCard | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [tab, setTab] = useState<Tab>(() => (sessionGet(`tab.${id}`) as Tab) || "templates");
  const [templates, setTemplates] = useState<Record<string, Template>>({});
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(() => sessionGet(`notice.${id}`));
  const [deleting, setDeleting] = useState(false);
  const [llmOn, setLlmOn] = useState(true);
  useEffect(() => { api.llmHealth().then((h) => setLlmOn(h.enabled !== false)).catch(() => { /* keep the default */ }); }, []);

  const dismissNotice = () => { setNotice(null); try { window.sessionStorage.removeItem(`notice.${id}`); } catch { /* ignore */ } };

  const [confirming, setConfirming] = useState(false);
  async function remove() {
    setDeleting(true);
    try {
      await api.remove(id);
      window.location.hash = "#/";
    } catch (e) { setError(String(e)); setDeleting(false); setConfirming(false); }
  }

  const load = useCallback(async () => {
    try {
      const r = await api.get(id);
      setA(r.assessment); setSc(r.scorecard); setJob(r.job);
    } catch (e) { setError(String(e)); }
  }, [id]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { sessionSet(`tab.${id}`, tab); }, [id, tab]);
  useEffect(() => {
    if (!a) return;
    const missing = a.templates.filter((t) => !templates[t]);
    if (missing.length) Promise.all(missing.map((t) => api.template(t))).then((ts) =>
      setTemplates((prev) => ({ ...prev, ...Object.fromEntries(ts.map((t) => [t.id, t])) })));
  }, [a, templates]);

  // poll while a job runs; keep polling through brief network errors and say so
  const active = isActive(job);
  useEffect(() => {
    if (!active) return;
    let misses = 0;
    const t = setInterval(async () => {
      try {
        const j = await api.job(id);
        misses = 0;
        setJob(j);
        if (!isActive(j)) { clearInterval(t); load(); }
      } catch (e) {
        if (++misses === 3) setError(`Lost contact with the server while the run is in progress (${e}). Still retrying…`);
      }
    }, 2000);
    return () => clearInterval(t);
  }, [active, id, load]);

  if (!a) return <><ErrorBox error={error} /><p className="muted">Loading…</p></>;
  const running = active && !job?.stale;

  return (
    <>
      <div className="row" style={{ marginBottom: 6 }}>
        <h1>{a.application.name}</h1>
        <span className="pill">{a.status}</span>
        <span className="muted mono">{a.id}</span>
        <span style={{ flex: 1 }} />
        {sc && <span style={{ color: RATING_COLOR[sc.rating], fontWeight: 700 }}>{sc.rating}</span>}
        {sc?.maturity_level && <span className="pill">L{sc.maturity_level} {sc.maturity_name}</span>}
        {a.status === "signed_off" || running
          ? <button disabled title={running ? "Cancel the run or wait for it to finish before editing" : "Signed off: details can no longer be changed"}>Edit</button>
          : <a className="btn" href={`#/a/${id}/edit`}>Edit</a>}
        <button className="danger" disabled={!!running || deleting} onClick={() => setConfirming(true)}
          title={running ? "Cancel the run or wait for it to finish before deleting" : "Delete this assessment permanently"}>
          {deleting ? "Deleting…" : "Delete"}
        </button>
      </div>
      <p className="muted small">Assessed {a.assessment_date} by {a.assessor || "—"} · {a.templates.length} templates · {a.findings.length} findings</p>
      <ErrorBox error={error} />
      {confirming && <ConfirmDialog title="Delete this assessment?" body={deleteMessage(a.application.name)} confirmLabel="Delete"
        busy={deleting} onConfirm={remove} onCancel={() => setConfirming(false)} />}
      {notice && (
        <p className="notice row" role="status" style={{ gap: 8 }}>
          <span style={{ flex: 1 }}>{notice}</span>
          <button className="link" onClick={dismissNotice}>Dismiss</button>
        </p>
      )}
      <JobPanel id={id} job={job} onCancel={async () => {
        try { setJob(await api.cancel(id)); } catch (e) { setError(String(e)); }
      }} />
      <div className="tabs" role="tablist">
        {TABS.map(([k, l]) => <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{l}</button>)}
      </div>
      {tab === "templates" && <TemplatesTab a={a} onSaved={load} setError={setError} />}
      {tab === "evidence" && <EvidenceTab a={a} running={!!running} llmOn={llmOn} setError={setError}
        onRun={async (llm) => { try { setJob(await api.run(id, llm)); } catch (e) { setError(String(e)); } }} />}
      {tab === "criteria" && <CriteriaTab a={a} templates={templates} llmOn={llmOn} onChange={(r) => { setSc(r.scorecard); load(); }} setError={setError} />}
      {tab === "findings" && <FindingsTab a={a} onChange={load} setError={setError} />}
      {tab === "score" && <ScoreTab sc={sc} templates={templates} />}
      {tab === "report" && <ReportTab a={a} onChange={load} setError={setError} />}
    </>
  );
}

function sessionGet(k: string) { try { return window.sessionStorage.getItem(k); } catch { return null; } }
function sessionSet(k: string, v: string) { try { window.sessionStorage.setItem(k, v); } catch { /* ignore */ } }

/* ------------------------------------------------------------------ templates */
function TemplatesTab({ a, onSaved, setError }: { a: Assessment; onSaved: () => void; setError: (e: string | null) => void }) {
  const [cands, setCands] = useState<Candidate[] | null>(null);
  const [sel, setSel] = useState<Set<string>>(new Set(a.templates));
  const [saving, setSaving] = useState(false);
  useEffect(() => { api.candidates(a.id).then((r) => setCands(r.candidates)).catch((e) => setError(String(e))); }, [a.id, setError]);
  const dirty = useMemo(() => [...sel].sort().join() !== [...a.templates].sort().join(), [sel, a.templates]);
  if (!cands) return <p className="muted">Scoring template applicability…</p>;
  return (
    <div className="card">
      <div className="row"><h2 style={{ margin: 0 }}>Proposed templates</h2><span style={{ flex: 1 }} />
        <button className="primary" disabled={!dirty || saving} onClick={async () => {
          setSaving(true);
          try { await api.setTemplates(a.id, cands.filter((c) => sel.has(c.template_id)).map((c) => c.template_id)); onSaved(); }
          catch (e) { setError(String(e)); } finally { setSaving(false); }
        }}>Save selection ({sel.size})</button>
      </div>
      <p className="muted small">Ranked from the application profile and the uploaded evidence. Confirm or adjust; the rubric's 18 P1/P2 criteria are always evaluated.</p>
      <table>
        <thead><tr><th></th><th>Template</th><th>Score</th><th>Why</th></tr></thead>
        <tbody>
          {cands.map((c) => (
            <tr key={c.template_id}>
              <td><input type="checkbox" aria-label={`Include ${c.template_id}`} checked={sel.has(c.template_id)} onChange={(e) => {
                const s = new Set(sel); if (e.target.checked) s.add(c.template_id); else s.delete(c.template_id); setSel(s);
              }} /></td>
              <td><b>{c.template_id}</b> {c.title}</td>
              <td>{c.always ? <span className="pill">always</span> : c.score}</td>
              <td className="small muted">{c.reasons.join("; ") || "No matching applicability rule"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ------------------------------------------------------------------ evidence */
const HINTS = ["", "runbook", "architecture", "dr_test", "dr_plan", "load_test", "slo", "error_budget", "capacity", "postmortem",
  "observability", "security", "incident", "change_request", "problem", "kb_knowledge", "em_alert", "cmdb_ci", "cmdb_rel_ci", "scan"];

function EvidenceTab({ a, running, llmOn, onRun, setError }: { a: Assessment; running: boolean; llmOn: boolean; onRun: (llm: boolean) => void; setError: (e: string | null) => void }) {
  const [ev, setEv] = useState<{ files: any[]; errors: string[]; summary: Record<string, number>; synced?: { table: string; rows: number; source: string }[]; notes?: string[] } | null>(null);
  const [hint, setHint] = useState("");
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [llm, setLlm] = useState(true);
  const input = useRef<HTMLInputElement>(null);
  const refresh = useCallback(() => api.evidence(a.id).then(setEv).catch((e) => setError(String(e))), [a.id, setError]);
  useEffect(() => { refresh(); }, [refresh]);
  async function upload(files: FileList | File[]) {
    if (!files.length) return;
    setBusy(true);
    try { await api.upload(a.id, files, hint || undefined); await refresh(); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  return (
    <>
      <div className="card">
        <h2>Upload evidence</h2>
        <p className="muted small">ServiceNow exports (CSV, XLSX or Table API JSON: incident, change_request, problem, kb_knowledge, em_alert, cmdb_ci, cmdb_rel_ci),
          documents (runbooks, architecture, DR/load tests, SLOs as PDF, DOCX, MD or TXT), screenshots and scans (JPG, PNG, TIFF, WebP: text is read
          with OCR), scanner output (Trivy, Grype, CycloneDX, SARIF), Kubernetes manifests and CI configuration. Secrets are redacted on ingest.</p>
        <div className="row" style={{ marginBottom: 10 }}>
          <label className="field">Type hint (optional)
            <select value={hint} onChange={(e) => setHint(e.target.value)}>{HINTS.map((h) => <option key={h} value={h}>{h || "auto-detect"}</option>)}</select>
          </label>
        </div>
        <div className={`dropzone ${over ? "over" : ""}`} onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); upload(e.dataTransfer.files); }}>
          {busy ? "Uploading…" : <>Drop files here or <button type="button" onClick={() => input.current?.click()}>choose files</button></>}
          <input ref={input} type="file" multiple hidden onChange={(e) => e.target.files && upload(e.target.files)} />
        </div>
      </div>
      <div className="card">
        <div className="row"><h2 style={{ margin: 0 }}>Evidence ({ev?.files.length ?? 0})</h2><span style={{ flex: 1 }} />
          {llmOn
            ? <label className="check"><input type="checkbox" checked={llm} onChange={(e) => setLlm(e.target.checked)} />Use local LLM for AI-assisted criteria</label>
            : <span className="muted small" title="LLM_ENABLED=false">Rules and checklists only (local LLM is off)</span>}
          <button className="primary" disabled={running || !(ev?.files.length || ev?.synced?.length)} onClick={() => onRun(llmOn && llm)}>{running ? "Running…" : "Run automated assessment"}</button>
        </div>
        {!!ev?.synced?.length && (
          <p className="muted small" style={{ marginTop: 8 }}>
            Also used, from the ServiceNow sync: {ev.synced.map((x) => `${x.table} (${x.rows} record${x.rows === 1 ? "" : "s"})`).join(", ")}.
            An uploaded export of a table replaces its synced copy.
          </p>
        )}
        {ev?.notes?.map((n) => <p key={n} className="muted small" style={{ marginTop: 4 }}>{n}</p>)}
        {ev?.errors.map((e) => <div key={e} className="notice small" style={{ marginTop: 8 }}>{e}</div>)}
        <table style={{ marginTop: 10 }}>
          <thead><tr><th>File</th><th>Detected as</th><th>Reviewed</th><th>Details</th></tr></thead>
          <tbody>
            {ev?.files.map((f) => (
              <tr key={f.id}>
                <td>{f.name}<div className="muted mono">{f.id}</div></td>
                <td>{f.table || f.doc_type || f.kind || "—"}</td>
                <td>{f.reviewed_at?.slice(0, 10) ?? "—"}</td>
                <td className="small muted">{Object.entries(f.meta || {}).map(([k, v]) => `${k}: ${v}`).join(", ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ criteria */
function CriteriaTab({ a, templates, llmOn, onChange, setError }: { a: Assessment; templates: Record<string, Template>; llmOn: boolean; onChange: (r: any) => void; setError: (e: string | null) => void }) {
  const [filter, setFilter] = useState<"all" | "review" | "gap" | "pending">("all");
  const all = a.templates.flatMap((t) => (templates[t]?.criteria ?? []).map((c) => ({ t, c })));
  const shown = all.filter(({ c }) => {
    const r = a.results[c.id];
    if (filter === "review") return r?.needs_human_review && !r?.reviewer;
    if (filter === "gap") return r?.result === "Gap" || r?.result === "Insufficient Evidence";
    if (filter === "pending") return !r || !r.result;
    return true;
  });
  const [cur, setCur] = useState<string | null>(null);
  const selected = all.find((x) => x.c.id === (cur ?? shown[0]?.c.id));
  const pending = all.filter(({ c }) => !a.results[c.id]?.result).length;
  const ran = Object.keys(a.detector_runs ?? {}).length > 0;
  return (
    <div className="split">
      <div className="card crit-list" style={{ maxHeight: "75vh", overflow: "auto" }}>
        {ran && pending > 0 && filter !== "pending" && (
          <p className="notice small" style={{ marginTop: 0 }}>
            {pending} criteria have no automated result{llmOn ? "" : " (the local LLM is off)"} and need your decision.{" "}
            <button className="link" onClick={() => { setFilter("pending"); setCur(null); }}>Show them</button>
          </p>
        )}
        <label className="field">Show
          <select value={filter} onChange={(e) => setFilter(e.target.value as any)}>
            <option value="all">All criteria ({all.length})</option><option value="review">Needs my review</option>
            <option value="gap">Gaps & insufficient</option><option value="pending">Not evaluated ({pending})</option>
          </select>
        </label>
        {a.templates.map((t) => {
          const items = shown.filter((x) => x.t === t);
          if (!items.length) return null;
          return (
            <div key={t}>
              <div className="tpl">{t} {templates[t]?.title}</div>
              {items.map(({ c }) => {
                const r = a.results[c.id];
                return (
                  <button key={c.id} className={selected?.c.id === c.id ? "on" : ""} onClick={() => setCur(c.id)}>
                    <ResultBadge r={r?.result} /> {r?.needs_human_review && !r?.reviewer ? "⚑ " : ""}<span className="small">{c.id.slice(-3)} {c.name}</span>
                  </button>
                );
              })}
            </div>
          );
        })}
      </div>
      {selected ? <CriterionPanel key={selected.c.id} a={a} cid={selected.c.id} c={selected.c} r={a.results[selected.c.id]} llmOn={llmOn} onChange={onChange} setError={setError} />
        : <div className="card muted">No criteria match this filter.</div>}
    </div>
  );
}

function CriterionPanel({ a, cid, c, r, llmOn, onChange, setError }: { a: Assessment; cid: string; c: any; r?: CriterionResult; llmOn: boolean; onChange: (r: any) => void; setError: (e: string | null) => void }) {
  const [form, setForm] = useState({ result: r?.result ?? "", risk: r?.risk ?? "", rationale: r?.rationale ?? "", recommendation: r?.recommendation ?? "",
    na_justification: r?.na_justification ?? "", na_approved: r?.na_approved ?? false,
    measured_value: r?.measured_value ?? "", maturity_level: r?.maturity_level ?? ("" as number | "") });
  const v2 = !!c.formula;
  const [q, setQ] = useState("");
  const [answer, setAnswer] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const findings = a.findings.filter((f) => f.criterion_ids.includes(cid));
  async function save() {
    setBusy(true);
    const { measured_value, maturity_level, ...base } = form;
    const body: Record<string, unknown> = { ...base, result: form.result || null, risk: form.risk || null };
    if (v2) Object.assign(body, { measured_value, maturity_level: maturity_level === "" ? null : Number(maturity_level) });
    try { onChange(await api.patchCriterion(a.id, cid, body)); setError(null); }
    catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  return (
    <div className="card">
      <div className="row"><h2 style={{ margin: 0 }}>{cid} · {c.name}</h2><ResultBadge r={r?.result} />
        {r?.source && <span className="pill">by {r.source}{r.reviewer ? ` · reviewed by ${r.reviewer}` : ""}</span>}
        {r?.confidence != null && r.source === "llm" && <span className="pill">confidence {Math.round(r.confidence * 100)}%</span>}
      </div>
      <dl className="small" style={{ display: "grid", gridTemplateColumns: "160px 1fr", gap: "4px 12px" }}>
        <dt className="muted">Acceptance criteria</dt><dd style={{ margin: 0 }}>{c.acceptance_criteria}</dd>
        <dt className="muted">Required evidence</dt><dd style={{ margin: 0 }}>{c.required_evidence}</dd>
        <dt className="muted">Evaluation method</dt><dd style={{ margin: 0 }}>{c.evaluation_method}</dd>
        <dt className="muted">Standard reference</dt><dd style={{ margin: 0 }}>{c.standard_ref || "—"}</dd>
        <dt className="muted">Default risk</dt><dd style={{ margin: 0 }}><Severity s={c.default_risk} /></dd>
        {v2 && <>
          <dt className="muted">Measurement / formula</dt><dd style={{ margin: 0 }}>{c.formula}</dd>
          <dt className="muted">Data source</dt><dd style={{ margin: 0 }} className="mono">{c.data_source}</dd>
          <dt className="muted">Maturity bands</dt><dd style={{ margin: 0 }}>{c.maturity_bands}</dd>
          <dt className="muted">Feeds rubric</dt><dd style={{ margin: 0 }}>{(c.rubric_refs ?? []).join(", ") || "—"}
            {!!c.rubric_enablers?.length && <span className="muted"> · enabler for {c.rubric_enablers.join(", ")}</span>}</dd>
          {c.look_for && <><dt className="muted">What to look for</dt><dd style={{ margin: 0 }}>{c.look_for}</dd></>}
        </>}
      </dl>
      {r?.rationale && <><h3>Automated rationale</h3><pre className="answer small">{r.rationale}</pre></>}
      {v2 && r?.measured_value && <p className="small"><b>Measured:</b> {r.measured_value}{r.maturity_level != null && <> · <b>Level</b> L{r.maturity_level}</>}</p>}
      {!!r?.evidence_refs?.length && <p className="small"><b>Evidence:</b> <span className="mono">{r.evidence_refs.join("; ")}</span></p>}
      {!!findings.length && <><h3>Findings ({findings.length})</h3><ul className="small">{findings.map((f) => <li key={f.id}><Severity s={f.severity} /> {f.title}</li>)}</ul></>}

      <h3 style={{ marginTop: 14 }}>Assessor decision</h3>
      <div className="row">
        <label className="field">Result
          <select value={form.result} onChange={(e) => setForm({ ...form, result: e.target.value as any })}>
            <option value="">—</option><option>Met</option><option>Gap</option><option>Insufficient Evidence</option><option>Not Applicable</option>
          </select>
        </label>
        <label className="field">Risk (if gap)
          <select value={form.risk} onChange={(e) => setForm({ ...form, risk: e.target.value as any })}>
            <option value="">default</option><option>Critical</option><option>High</option><option>Medium</option><option>Low</option>
          </select>
        </label>
        {v2 && <label className="field">Maturity level
          <select value={String(form.maturity_level)} onChange={(e) => setForm({ ...form, maturity_level: e.target.value === "" ? "" : Number(e.target.value) })}>
            <option value="">not assessed</option>{[0, 1, 2, 3, 4].map((l) => <option key={l} value={l}>L{l}</option>)}
          </select>
        </label>}
        {v2 && <label className="field" style={{ flex: 1 }}>Measured value
          <input value={form.measured_value} onChange={(e) => setForm({ ...form, measured_value: e.target.value })} placeholder="e.g. 87% of 214 resolved incidents" />
        </label>}
        {form.result === "Not Applicable" && <label className="check"><input type="checkbox" checked={form.na_approved}
          onChange={(e) => setForm({ ...form, na_approved: e.target.checked })} />N/A approved</label>}
      </div>
      {form.result === "Not Applicable" && <label className="field" style={{ marginTop: 8 }}>N/A justification (required)
        <textarea value={form.na_justification} onChange={(e) => setForm({ ...form, na_justification: e.target.value })} /></label>}
      <label className="field" style={{ marginTop: 8 }}>Rationale<textarea value={form.rationale} onChange={(e) => setForm({ ...form, rationale: e.target.value })} /></label>
      <label className="field" style={{ marginTop: 8 }}>Recommendation (written to the template)<textarea value={form.recommendation} onChange={(e) => setForm({ ...form, recommendation: e.target.value })} /></label>
      <div className="row" style={{ marginTop: 10 }}>
        <button className="primary" disabled={busy} onClick={save}>Save decision</button>
        {r?.result && r.source !== "human" && <button disabled={busy} onClick={async () => {
          setBusy(true); try { onChange(await api.confirm(a.id, cid)); } catch (e) { setError(String(e)); } finally { setBusy(false); }
        }}>Confirm automated result</button>}
        <span className="muted small">Signed in as {getUser() || "anonymous"}</span>
      </div>

      {llmOn && <>
      <h3 style={{ marginTop: 16 }}>Ask about this criterion</h3>
      <div className="row">
        <input style={{ flex: 1 }} value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. Which runbook covers the settlement job?" />
        <button disabled={!q || busy} onClick={async () => {
          setBusy(true); setAnswer(null);
          try { setAnswer((await api.chat(a.id, cid, q)).answer); } catch (e) { setAnswer(String(e)); } finally { setBusy(false); }
        }}>Ask</button>
      </div>
      {answer && <pre className="answer small">{answer}</pre>}
      </>}
    </div>
  );
}

/* ------------------------------------------------------------------ findings */
function FindingsTab({ a, onChange, setError }: { a: Assessment; onChange: () => void; setError: (e: string | null) => void }) {
  const [type, setType] = useState("all");
  const [open, setOpen] = useState<string | null>(null);
  const order = { Critical: 0, High: 1, Medium: 2, Low: 3 } as const;
  const rows = a.findings.filter((f) => type === "all" || f.type === type).sort((x, y) => order[x.severity] - order[y.severity]);
  const counts = a.findings.reduce<Record<string, number>>((m, f) => ({ ...m, [f.type]: (m[f.type] ?? 0) + 1 }), {});
  return (
    <div className="card">
      <div className="row">
        <h2 style={{ margin: 0 }}>Findings</h2>
        {["all", "gap", "vulnerability", "anomaly", "observation"].map((t) => (
          <button key={t} className={type === t ? "primary" : ""} onClick={() => setType(t)}>{t} {t === "all" ? a.findings.length : counts[t] ?? 0}</button>
        ))}
      </div>
      <table style={{ marginTop: 10 }}>
        <thead><tr><th>Severity</th><th>Finding</th><th>Affects</th><th>Recommendation</th><th>Owner / due</th><th>Status</th></tr></thead>
        <tbody>
          {rows.map((f) => (
            <FindingRow key={f.id} a={a} f={f} open={open === f.id} toggle={() => setOpen(open === f.id ? null : f.id)} onChange={onChange} setError={setError} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FindingRow({ a, f, open, toggle, onChange, setError }: { a: Assessment; f: Finding; open: boolean; toggle: () => void; onChange: () => void; setError: (e: string | null) => void }) {
  const [action, setAction] = useState(f.recommendation?.action ?? "");
  const [owner, setOwner] = useState(f.recommendation?.owner ?? "");
  const [due, setDue] = useState(f.recommendation?.due_date ?? "");
  const [reason, setReason] = useState("");
  const patch = async (p: Record<string, unknown>) => { try { await api.patchFinding(a.id, f.id, p); onChange(); } catch (e) { setError(String(e)); } };
  return (
    <>
      <tr className="clickable" onClick={toggle}>
        <td><Severity s={f.severity} /><div className="muted small">{f.type}</div></td>
        <td>{f.title}<div className="muted small mono">{f.detector}</div></td>
        <td className="small">{[...f.rubric_ids, ...f.criterion_ids].slice(0, 4).join(", ")}{f.criterion_ids.length + f.rubric_ids.length > 4 ? "…" : ""}</td>
        <td className="small">{f.recommendation?.action}<div className="muted">{[...(f.recommendation?.playbooks ?? []), ...(f.recommendation?.playbook_steps ?? [])].join(" · ")}</div></td>
        <td className="small">{f.recommendation?.owner}<div className="muted">{f.recommendation?.due_date}</div></td>
        <td><span className="pill">{f.status}</span></td>
      </tr>
      {open && (
        <tr><td colSpan={6} style={{ background: "var(--surface-2)" }}>
          {f.detail && <pre className="answer small">{f.detail}</pre>}
          {!!f.evidence_refs.length && <p className="small mono">{f.evidence_refs.join("; ")}</p>}
          <div className="grid">
            <label className="field">Action<textarea value={action} onChange={(e) => setAction(e.target.value)} /></label>
            <label className="field">Owner<input value={owner} onChange={(e) => setOwner(e.target.value)} /></label>
            <label className="field">Due date<input type="date" value={due ?? ""} onChange={(e) => setDue(e.target.value)} /></label>
            <label className="field">Reason (required to dispute)<input value={reason} onChange={(e) => setReason(e.target.value)} /></label>
          </div>
          <div className="row" style={{ marginTop: 8 }}>
            <button className="primary" onClick={() => patch({ recommendation: { action, owner, due_date: due || null } })}>Save recommendation</button>
            <button onClick={() => patch({ status: "accepted" })}>Accept finding</button>
            <button disabled={!reason} onClick={() => patch({ status: "disputed", reason })}>Dispute</button>
            <button onClick={() => patch({ status: "resolved", reason: reason || undefined })}>Mark resolved</button>
          </div>
        </td></tr>
      )}
    </>
  );
}

/* ------------------------------------------------------------------ score */
function ScoreTab({ sc, templates }: { sc: ScoreCard | null; templates: Record<string, Template> }) {
  if (!sc) return <div className="card muted">Run the automated assessment to compute scores.</div>;
  return (
    <>
      <div className="grid">
        <div className="card"><div className="muted small">Readiness rating</div><div className="kpi" style={{ color: RATING_COLOR[sc.rating] }}>{sc.rating}</div><div className="small muted">{sc.rating_outcome}</div></div>
        <div className="card"><div className="muted small">Reliability Readiness Index</div><div className="kpi">{sc.index ?? "—"}</div><Bar value={sc.index} /></div>
        <div className="card"><div className="muted small">Maturity</div><div className="kpi">L{sc.maturity_level} {sc.maturity_name}</div>
          <div className="small muted">{sc.maturity_capped ? `Capped by "${sc.rating}" rating` : "Not capped"}</div></div>
        <div className="card"><div className="muted small">Evidence coverage</div><div className="kpi">{sc.evidence_coverage_pct}%</div><Bar value={sc.evidence_coverage_pct} /></div>
      </div>
      <div className="card">
        <h2>Domain scores</h2>
        {Object.entries(sc.domain_scores).map(([d, v]) => <div key={d} className="row" style={{ marginBottom: 6 }}><span style={{ width: 60 }}>{d}</span><div style={{ flex: 1 }}><Bar value={v} /></div><span style={{ width: 50, textAlign: "right" }}>{v}</span></div>)}
        <p className="muted small">Weights SRE 50% · ITSM 30% · CMDB 20% (rubric), renormalised over assessed domains. Matrix {sc.matrix_version}.</p>
      </div>
      <div className="card">
        <h2>Rubric criteria — P1 gaps {sc.p1_gaps.length} · P2 gaps {sc.p2_gaps.length}</h2>
        <table><thead><tr><th>ID</th><th>Criterion</th><th>Domain</th><th>Result</th><th>Based on</th></tr></thead>
          <tbody>{sc.rubric.map((o) => <tr key={o.rubric_id}><td>{o.rubric_id}</td><td>{o.name}</td><td>{o.domain}</td><td><ResultBadge r={o.result} /></td><td className="small muted">{o.contributing.join(", ") || "no evidence"}</td></tr>)}</tbody>
        </table>
      </div>
      <div className="card">
        <h2>Template scores</h2>
        <table><thead><tr><th>Template</th><th>Score</th><th>Met</th><th>Gap</th><th>Insufficient</th><th>N/A</th><th>Not evaluated</th></tr></thead>
          <tbody>{sc.template_scores.map((t) => <tr key={t.template_id}><td><b>{t.template_id}</b> {templates[t.template_id]?.title}</td><td style={{ width: 160 }}>{t.score ?? "—"} <Bar value={t.score} /></td><td>{t.met}</td><td>{t.gaps}</td><td>{t.insufficient}</td><td>{t.na}</td><td>{t.unevaluated}</td></tr>)}</tbody>
        </table>
      </div>
      {sc.cmdb && <CmdbCard cm={sc.cmdb} />}
      {!!sc.conflicts.length && <div className="card"><h2>Threshold conflicts</h2><ul className="small">{sc.conflicts.map((c) => <li key={c}>{c}</li>)}</ul></div>}
    </>
  );
}

function CmdbCard({ cm }: { cm: CmdbReadiness }) {
  const tone = { passed: "r-Met", "not passed": "r-Gap", undetermined: "r-Insufficient" } as const;
  return (
    <div className="card">
      <div className="row"><h2 style={{ margin: 0 }}>CMDB / CSDM readiness</h2><span className="pill">CMDB-AT-13 · V2</span></div>
      <div className="grid" style={{ marginTop: 10 }}>
        <div><div className="muted small">CMDB Readiness Index</div><div className="kpi">{cm.cri ?? "—"}</div><Bar value={cm.cri} />
          <div className="small muted">{cm.cri == null ? "Set maturity levels on CMDB criteria to compute it" : cm.complete ? "All CRI criteria assessed" : `Partial: ${cm.assessed} of ${cm.total} criteria assessed`}</div></div>
        <div><div className="muted small">Indicative maturity</div><div className="kpi">{cm.level == null ? "—" : `L${cm.level} ${cm.level_name}`}</div>
          <div className="small muted">{cm.thresholds_status}</div></div>
      </div>
      <table style={{ marginTop: 10 }}><thead><tr><th>Dimension</th><th>Weight</th><th>Score</th><th>Assessed</th></tr></thead>
        <tbody>{cm.dimensions.map((d) => <tr key={d.name}><td>{d.name}</td><td>{Math.round(d.weight * 100)}%</td>
          <td style={{ width: 160 }}>{d.score ?? "—"} <Bar value={d.score} /></td>
          <td className="small">{d.assessed} of {d.total}{!!d.missing.length && <div className="muted">needs a level: {d.missing.join(", ")}</div>}</td></tr>)}</tbody>
      </table>
      <h3 style={{ marginTop: 14 }}>Reporting gates</h3>
      <table><thead><tr><th>Gate</th><th>Status</th><th>Unlocks</th><th>Why</th></tr></thead>
        <tbody>{cm.gates.map((g) => <tr key={g.id}><td><b>{g.id}</b> {g.name}</td><td><span className={`badge ${tone[g.status]}`}>{g.status}</span></td>
          <td className="small">{g.unlocks}</td>
          <td className="small muted">{[...g.failed, ...g.pending.map((p) => `pending ${p}`)].join("; ") || "all conditions met"}</td></tr>)}</tbody>
      </table>
    </div>
  );
}

/* ------------------------------------------------------------------ report */
function ReportTab({ a, onChange, setError }: { a: Assessment; onChange: () => void; setError: (e: string | null) => void }) {
  const [gate, setGate] = useState<{ ok: boolean; blockers: string[]; warnings: string[] } | null>(null);
  const [names, setNames] = useState<Record<string, string>>({});
  const [downloading, setDownloading] = useState(false);
  useEffect(() => { api.gate(a.id).then(setGate).catch((e) => setError(String(e))); }, [a, setError]);
  async function download() {
    setDownloading(true);
    try {
      const r = await fetch(api.reportUrl(a.id), { headers: getUser() ? { "X-User": getUser() } : {} });
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url; link.download = `SRA ${a.application.name} ${a.assessment_date}.zip`; link.click();
      URL.revokeObjectURL(url);
    } catch (e) { setError(String(e)); } finally { setDownloading(false); }
  }
  return (
    <>
      <div className="card">
        <div className="row"><h2 style={{ margin: 0 }}>Completeness gate</h2>{gate && <span className={`badge ${gate.ok ? "r-Met" : "r-Gap"}`}>{gate.ok ? "Passed" : `${gate.blockers.length} blockers`}</span>}</div>
        {gate && !gate.ok && <ul className="small">{gate.blockers.slice(0, 30).map((b) => <li key={b}>{b}</li>)}{gate.blockers.length > 30 && <li>… {gate.blockers.length - 30} more</li>}</ul>}
        {gate && !!gate.warnings.length && <details><summary className="small">{gate.warnings.length} warnings</summary><ul className="small">{gate.warnings.map((w) => <li key={w}>{w}</li>)}</ul></details>}
        <div className="row" style={{ marginTop: 10 }}>
          <button className="primary" disabled={downloading} onClick={download}>{downloading ? "Generating…" : "Download filled templates + summary (.zip)"}</button>
          {gate && !gate.ok && <span className="muted small">A draft can be downloaded now; it is marked incomplete until the gate passes.</span>}
        </div>
      </div>
      <div className="card">
        <h2>Sign-off</h2>
        <table><thead><tr><th>Role</th><th>Name</th><th>Date</th><th>Decision</th><th></th></tr></thead>
          <tbody>{a.signoffs.map((s) => (
            <tr key={s.role}><td>{s.role}</td>
              <td>{s.name || <input aria-label={`${s.role} name`} value={names[s.role] ?? ""} onChange={(e) => setNames({ ...names, [s.role]: e.target.value })} />}</td>
              <td>{s.date ?? "—"}</td><td>{s.decision || "—"}</td>
              <td>{!s.name && <button disabled={!gate?.ok || !names[s.role]} onClick={async () => {
                try { await api.signoff(a.id, s.role, names[s.role], "Approved"); onChange(); } catch (e) { setError(String(e)); }
              }}>Approve</button>}</td>
            </tr>))}
          </tbody>
        </table>
        {!gate?.ok && <p className="muted small">Sign-off opens once every criterion is evaluated and every gap has a recommendation.</p>}
      </div>
    </>
  );
}
