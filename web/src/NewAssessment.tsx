import { useState } from "react";
import { api } from "./api";
import { ErrorBox } from "./ui";

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

export default function NewAssessment() {
  const [p, setP] = useState<Record<string, any>>({ name: "", cmdb_id: "", aliases: "", tier: 2, priority_class: "P2", trigger: "periodic", hosting: "cloud", ci_tool: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: string, v: unknown) => setP((x) => ({ ...x, [k]: v }));

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      const profile = { ...p, aliases: String(p.aliases || "").split(",").map((s) => s.trim()).filter(Boolean),
        tier: p.tier ? Number(p.tier) : null, ci_tool: p.ci_tool || null };
      const r = await api.create(profile);
      window.location.hash = `#/a/${r.assessment.id}`;
    } catch (err) { setError(String(err)); } finally { setBusy(false); }
  }

  return (
    <form onSubmit={submit}>
      <h1 style={{ marginBottom: 14 }}>New assessment</h1>
      <ErrorBox error={error} />
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
        <p className="muted small">These drive which of the 39 assessment templates apply. Counts and some flags are also derived from the evidence you upload.</p>
        <div className="grid">
          {FLAGS.map(([k, l]) => (
            <label key={k} className="check"><input type="checkbox" checked={!!p[k]} onChange={(e) => set(k, e.target.checked)} />{l}</label>
          ))}
        </div>
      </div>
      <button className="primary" disabled={busy || !p.name}>{busy ? "Creating…" : "Create and propose templates"}</button>
    </form>
  );
}
