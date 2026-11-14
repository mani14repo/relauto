import { DocumentImport } from "@carbon/icons-react";
import { Button } from "@carbon/react";
import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { ErrorBox, Notice } from "./ui";

const TRIGGERS = [
  ["new_production", "Entering production"], ["major_change", "Major change"], ["architecture_change", "Architecture change"],
  ["deployment", "Production deployment"], ["post_incident", "Post-incident review"], ["periodic", "Periodic assessment"],
];
const FLAGS: [string, string][] = [
  ["containerized", "Containerised / Kubernetes"], ["has_cicd", "Has CI/CD pipeline"], ["external_integrations", "External integrations"],
  ["cross_team_dependencies", "Cross-team dependencies"], ["batch_or_peak_load", "Batch or peak-load risk"], ["vendors", "Third-party vendors"],
  ["regulated", "Regulated (FSI controls)"], ["has_sla", "Defined SLAs"], ["uses_pse_queue", "Handled by PSE (Tier-1)"],
  ["has_reliability_automation", "Reliability automation in prod"], ["sre_toil_work", "SRE/AE operational toil"],
  ["eol_components", "Known EOL components"], ["service_catalog_scope", "Service catalog scope"],
];
const BLANK = { name: "", cmdb_id: "", aliases: "", tier: 2, priority_class: "P2", trigger: "periodic", hosting: "cloud", ci_tool: "" };

/** The form state for a profile: aliases as one comma-separated string, empty strings for unset values. */
function toForm(profile: Record<string, any>): Record<string, any> {
  return { ...BLANK, ...profile, aliases: (profile.aliases ?? []).join(", "), tier: profile.tier ?? "", ci_tool: profile.ci_tool ?? "" };
}

function toProfile(p: Record<string, any>): Record<string, unknown> {
  return { ...p, aliases: String(p.aliases || "").split(",").map((s) => s.trim()).filter(Boolean),
    tier: p.tier ? Number(p.tier) : null, ci_tool: p.ci_tool || null };
}

/** "Fill from profile.yaml": reads the file on the server and replaces the form's profile fields with it. */
function ProfileImport({ onLoad }: { onLoad: (profile: Record<string, any>) => void }) {
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  async function pick(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    e.target.value = "";  // picking the same file again re-reads it
    if (!f) return;
    setBusy(true); setError(null); setMsg(null);
    try {
      const r = await api.parseProfile(f);
      onLoad(r.profile);
      setMsg(`Filled from ${r.filename}. Check the values before continuing.` +
        (r.ignored.length ? ` Ignored unknown fields: ${r.ignored.join(", ")}.` : "") +
        (r.profile.name ? "" : " The file has no name; enter one."));
    } catch (err) { setError(String(err)); } finally { setBusy(false); }
  }

  return (
    <div className="card">
      <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
        <Button kind="tertiary" size="md" renderIcon={DocumentImport} disabled={busy} onClick={() => input.current?.click()}>
          {busy ? "Reading…" : "Fill from profile.yaml…"}
        </Button>
        <input ref={input} type="file" accept=".yaml,.yml,.json" onChange={pick} disabled={busy} hidden aria-label="profile.yaml file" />
        <span className="muted small">Optional. Replaces the fields below with the file's values; anything the file leaves out goes back to its default.</span>
      </div>
      <ErrorBox error={error} />
      {msg && <div style={{ marginTop: 12 }}><Notice kind="success">{msg}</Notice></div>}
    </div>
  );
}

function ProfileFields({ p, set }: { p: Record<string, any>; set: (k: string, v: unknown) => void }) {
  return (
    <>
      <div className="card">
        <h2>Application</h2>
        <div className="grid">
          <label className="field">Application name *<input required value={p.name} onChange={(e) => set("name", e.target.value)} /></label>
          <label className="field">CMDB ID<input value={p.cmdb_id} onChange={(e) => set("cmdb_id", e.target.value)} /></label>
          <label className="field">Other names / support groups (comma-separated)<input value={p.aliases} onChange={(e) => set("aliases", e.target.value)} placeholder="Orders Support, ORD-BATCH" /></label>
          <label className="field">Service tier
            <select value={p.tier} onChange={(e) => set("tier", e.target.value)}>
              <option value="1">Tier 1 — Critical</option><option value="2">Tier 2 — Standard</option><option value="3">Tier 3</option><option value="">Unknown</option>
            </select>
          </label>
          <label className="field">Application class
            <select value={p.priority_class} onChange={(e) => set("priority_class", e.target.value)}><option>P1</option><option>P2</option><option>P3</option></select>
          </label>
          <label className="field">Assessment trigger
            <select value={p.trigger} onChange={(e) => set("trigger", e.target.value)}>{TRIGGERS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
          </label>
          <label className="field">Hosting
            <select value={p.hosting} onChange={(e) => set("hosting", e.target.value)}><option value="cloud">Cloud</option><option value="on_prem">On-premises</option><option value="hybrid">Hybrid</option></select>
          </label>
          <label className="field">CI tool<input value={p.ci_tool} onChange={(e) => set("ci_tool", e.target.value)} placeholder="github_actions, jenkins…" /></label>
        </div>
      </div>
      <div className="card">
        <h2>Characteristics</h2>
        <p className="muted small">These drive which of the 41 assessment templates apply. Counts and some flags are also derived from the evidence you upload.</p>
        <div className="grid">
          {FLAGS.map(([k, l]) => (
            <label key={k} className="check"><input type="checkbox" checked={!!p[k]} onChange={(e) => set(k, e.target.checked)} />{l}</label>
          ))}
        </div>
      </div>
    </>
  );
}

export default function NewAssessment() {
  const [p, setP] = useState<Record<string, any>>(BLANK);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: string, v: unknown) => setP((x) => ({ ...x, [k]: v }));

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      const r = await api.create(toProfile(p));
      window.location.hash = `#/a/${r.assessment.id}`;
    } catch (err) { setError(String(err)); } finally { setBusy(false); }
  }

  return (
    <form onSubmit={submit}>
      <div className="page-head">
        <div><p className="page-eyebrow"><a href="#/">Reliability assessments</a></p><h1>New assessment</h1></div>
      </div>
      <ErrorBox error={error} />
      <ProfileImport onLoad={(x) => setP(toForm(x))} />
      <ProfileFields p={p} set={set} />
      <Button type="submit" size="lg" disabled={busy || !p.name}>{busy ? "Creating…" : "Create and propose templates"}</Button>
    </form>
  );
}

export function EditAssessment({ id }: { id: string }) {
  const [p, setP] = useState<Record<string, any> | null>(null);
  const [date, setDate] = useState("");
  const [assessor, setAssessor] = useState("");
  const [locked, setLocked] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: string, v: unknown) => setP((x) => ({ ...x, [k]: v }));
  const back = `#/a/${id}`;

  useEffect(() => {
    api.get(id).then(({ assessment: a, job }) => {
      setP(toForm(a.profile as Record<string, any>));
      setDate(a.assessment_date);
      setAssessor(a.assessor);
      if (a.status === "signed_off") setLocked("This assessment is signed off; its details can no longer be changed.");
      else if ((job?.state === "queued" || job?.state === "running") && !job?.stale) setLocked("An automated pass is running. Cancel it or wait for it to finish before editing.");
    }).catch((e) => setError(String(e)));
  }, [id]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!p) return;
    setBusy(true); setError(null);
    try {
      const r = await api.edit(id, { profile: toProfile(p), assessment_date: date, assessor });
      if (r.rerun_needed) {
        try { window.sessionStorage.setItem(`notice.${id}`, "Details changed. Check the Templates tab, then run the automated pass again so the results reflect them."); } catch { /* ignore */ }
      }
      window.location.hash = back;
    } catch (err) { setError(String(err)); } finally { setBusy(false); }
  }

  if (!p) return <><ErrorBox error={error} /><p className="muted">Loading…</p></>;
  return (
    <form onSubmit={submit}>
      <div className="page-head">
        <div><p className="page-eyebrow"><a href="#/">Reliability assessments</a> / <a href={back} className="mono">{id}</a></p><h1>Edit assessment</h1></div>
      </div>
      <ErrorBox error={error} />
      {locked && <Notice kind="warning">{locked}</Notice>}
      <fieldset disabled={!!locked || busy} style={{ border: "none", padding: 0, margin: 0 }}>
        <div className="card">
          <h2>Assessment</h2>
          <div className="grid">
            <label className="field">Assessment date<input type="date" required value={date} onChange={(e) => setDate(e.target.value)} /></label>
            <label className="field">Assessor<input value={assessor} onChange={(e) => setAssessor(e.target.value)} /></label>
          </div>
          <p className="muted small">The date sets the window the rules look back over (e.g. incidents in the last 90 days).</p>
        </div>
        <ProfileImport onLoad={(x) => setP(toForm(x))} />
        <ProfileFields p={p} set={set} />
        <div className="btn-row">
          <Button type="submit" size="lg" disabled={!p.name || !date}>{busy ? "Saving…" : "Save changes"}</Button>
          <Button kind="secondary" size="lg" href={back}>Cancel</Button>
        </div>
      </fieldset>
    </form>
  );
}
