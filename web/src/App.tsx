import { useEffect, useState } from "react";
import { api, getUser, setUser } from "./api";
import AssessmentView from "./AssessmentView";
import NewAssessment from "./NewAssessment";
import { ErrorBox, RATING_COLOR } from "./ui";

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
  useEffect(() => { api.list().then(setRows).catch((e) => setError(String(e))); }, []);
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
            <thead><tr><th>Application</th><th>Date</th><th>Status</th><th>Templates</th><th>Findings</th><th>Rating</th><th>Index</th><th>Maturity</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className="clickable" onClick={() => (window.location.hash = `#/a/${r.id}`)}>
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
    </>
  );
}

export default function App() {
  const hash = useHash();
  const [user, setU] = useState(getUser());
  const [llm, setLlm] = useState<{ model: string; reachable: boolean } | null>(null);
  useEffect(() => { api.llmHealth().then(setLlm).catch(() => setLlm(null)); }, []);
  const m = hash.match(/^#\/a\/(SRA-[0-9a-f]+)/);
  return (
    <>
      <header className="top">
        <a href="#/">SRA · Site Reliability Assessment</a>
        <span className="spacer" />
        {llm && <span className="small" title="Local LLM used for AI-assisted criteria">LLM {llm.model}: {llm.reachable ? "ready" : "unavailable"}</span>}
        <input aria-label="Your name or email" placeholder="your.name@company.com" value={user}
          onChange={(e) => { setU(e.target.value); setUser(e.target.value); }} />
      </header>
      <main>
        {hash.startsWith("#/new") ? <NewAssessment /> : m ? <AssessmentView id={m[1]} key={m[1]} /> : <Home />}
      </main>
    </>
  );
}
