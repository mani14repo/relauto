import { useEffect, useState } from "react";
import { api, getUser, setUser } from "./api";
import AssessmentView from "./AssessmentView";
import NewAssessment, { EditAssessment } from "./NewAssessment";
import { ConfirmDialog, deleteMessage, ErrorBox, RATING_COLOR, TrashIcon } from "./ui";

function useHash() {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const on = () => setHash(window.location.hash);
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

function Home() {
  const [rows, setRows] = useState<any[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [doomed, setDoomed] = useState<{ id: string; application: string } | null>(null);
  const [deleting, setDeleting] = useState(false);
  useEffect(() => { api.list().then(setRows).catch((e) => setError(String(e))); }, []);

  async function confirmDelete() {
    if (!doomed) return;
    setDeleting(true); setError(null);
    try {
      await api.remove(doomed.id);
      setRows((rs) => (rs ?? []).filter((r) => r.id !== doomed.id));
    } catch (e) { setError(`${doomed.application} was not deleted. ${e}`); }
    setDeleting(false); setDoomed(null);
  }
  return (
    <>
      <div className="row" style={{ marginBottom: 14 }}>
        <h1>Reliability assessments</h1>
        <span style={{ flex: 1 }} />
        <a className="btn primary" href="#/new">New assessment</a>
      </div>
      <ErrorBox error={error} />
      <div className="card">
        {rows === null ? <p className="muted">Loading…</p> : rows.length === 0 ? (
          <p className="muted">No assessments yet. Start one to select templates, upload evidence and score readiness.</p>
        ) : (
          <table>
            <thead><tr><th className="row-actions"><span className="sr-only">Delete</span></th><th>Application</th><th>Date</th><th>Status</th><th>Templates</th><th>Findings</th><th>Rating</th><th>Index</th><th>Maturity</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className="clickable" onClick={() => (window.location.hash = `#/a/${r.id}`)}>
                  <td className="row-actions">
                    <button type="button" className="icon" title={`Delete ${r.application}`} aria-label={`Delete ${r.application}`}
                      onClick={(e) => { e.stopPropagation(); setDoomed({ id: r.id, application: r.application }); }}><TrashIcon /></button>
                  </td>
                  <td><a href={`#/a/${r.id}`}>{r.application}</a><div className="muted mono">{r.id}</div></td>
                  <td>{r.date}</td><td>{r.status}</td><td>{r.templates}</td><td>{r.findings}</td>
                  <td style={{ color: RATING_COLOR[r.rating] ?? "inherit", fontWeight: 600 }}>{r.rating ?? "—"}</td>
                  <td>{r.index ?? "—"}</td><td>{r.maturity ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {doomed && <ConfirmDialog title="Delete this assessment?" body={deleteMessage(doomed.application)} confirmLabel="Delete"
        busy={deleting} onConfirm={confirmDelete} onCancel={() => setDoomed(null)} />}
    </>
  );
}

export default function App() {
  const hash = useHash();
  const [user, setU] = useState(getUser());
  const [llm, setLlm] = useState<{ enabled: boolean; model: string | null; reachable: boolean } | null>(null);
  useEffect(() => { api.llmHealth().then(setLlm).catch(() => setLlm(null)); }, []);
  const m = hash.match(/^#\/a\/(SRA-[0-9a-f]+)/);
  const editing = /^#\/a\/SRA-[0-9a-f]+\/edit/.test(hash);
  return (
    <>
      <header className="top">
        <a href="#/">SRA · Site Reliability Assessment</a>
        <span className="spacer" />
        {llm && (llm.enabled === false
          ? <span className="small" title="LLM_ENABLED=false: runs are rules only and criterion chat is off">LLM off</span>
          : <span className="small" title="Local LLM used for AI-assisted criteria">LLM {llm.model}: {llm.reachable ? "ready" : "unavailable"}</span>)}
        <input aria-label="Your name or email" placeholder="your.name@company.com" value={user}
          onChange={(e) => { setU(e.target.value); setUser(e.target.value); }} />
      </header>
      <main>
        {hash.startsWith("#/new") ? <NewAssessment /> : m && editing ? <EditAssessment id={m[1]} key={`edit-${m[1]}`} />
          : m ? <AssessmentView id={m[1]} key={m[1]} /> : <Home />}
      </main>
    </>
  );
}
