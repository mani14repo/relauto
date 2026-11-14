import { Add } from "@carbon/icons-react";
import { Button, IconButton } from "@carbon/react";
import { useEffect, useState } from "react";
import { api } from "./api";
import AssessmentView from "./AssessmentView";
import NewAssessment, { EditAssessment } from "./NewAssessment";
import Shell, { type NavKey } from "./Shell";
import { AsOf, ConfirmDialog, deleteMessage, ErrorBox, Rating, TrashIcon } from "./ui";

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
  const [loadedAt, setLoadedAt] = useState<Date | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [doomed, setDoomed] = useState<{ id: string; application: string } | null>(null);
  const [deleting, setDeleting] = useState(false);
  useEffect(() => { api.list().then((r) => { setRows(r); setLoadedAt(new Date()); }).catch((e) => setError(String(e))); }, []);

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
      <div className="page-head">
        <div>
          <p className="page-eyebrow">Reliability (SRE)</p>
          <h1>Reliability assessments</h1>
        </div>
        <span style={{ flex: 1 }} />
        <Button href="#/new" renderIcon={Add}>New assessment</Button>
      </div>
      <ErrorBox error={error} />
      <div className="card">
        <div className="card-head"><h2>Assessments{rows ? ` (${rows.length})` : ""}</h2><AsOf at={loadedAt} /></div>
        {rows === null ? <p className="muted">Loading…</p> : rows.length === 0 ? (
          <p className="muted">No assessments yet. Start one to select templates, upload evidence and score readiness.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th className="row-actions"><span className="sr-only">Delete</span></th><th>Application</th><th>Date</th><th>Status</th><th>Templates</th><th>Findings</th><th>Rating</th><th>Index</th><th>Maturity</th></tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className="clickable" onClick={() => (window.location.hash = `#/a/${r.id}`)}>
                    <td className="row-actions">
                      <IconButton kind="ghost" size="sm" label={`Delete ${r.application}`} align="right" className="row-delete"
                        onClick={(e: React.MouseEvent) => { e.stopPropagation(); setDoomed({ id: r.id, application: r.application }); }}><TrashIcon /></IconButton>
                    </td>
                    <td><a href={`#/a/${r.id}`}>{r.application}</a><div className="muted mono">{r.id}</div></td>
                    <td>{r.date}</td><td>{r.status}</td><td className="num">{r.templates}</td><td className="num">{r.findings}</td>
                    <td><Rating r={r.rating} /></td>
                    <td className="num">{r.index ?? "—"}</td><td>{r.maturity ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {doomed && <ConfirmDialog title="Delete this assessment?" body={deleteMessage(doomed.application)} confirmLabel="Delete"
        busy={deleting} onConfirm={confirmDelete} onCancel={() => setDoomed(null)} />}
    </>
  );
}

export default function App() {
  const hash = useHash();
  const m = hash.match(/^#\/a\/(SRA-[0-9a-f]+)/);
  const editing = /^#\/a\/SRA-[0-9a-f]+\/edit/.test(hash);
  // The open assessment gets its own nav entry; the view reports its application name once loaded.
  const [current, setCurrent] = useState<{ id: string; label: string } | null>(null);
  useEffect(() => { if (m && current?.id !== m[1]) setCurrent({ id: m[1], label: m[1] }); }, [m?.[1]]); // eslint-disable-line react-hooks/exhaustive-deps
  const active: NavKey = hash.startsWith("#/new") ? "new" : m ? "assessment" : "home";
  const named = (id: string, label: string) => setCurrent((c) => (c?.id === id && c.label === label ? c : { id, label }));
  return (
    <Shell active={active} current={m && current?.id === m[1] ? current : null}>
      {hash.startsWith("#/new") ? <NewAssessment /> : m && editing ? <EditAssessment id={m[1]} key={`edit-${m[1]}`} />
        : m ? <AssessmentView id={m[1]} key={m[1]} onName={(n) => named(m[1], n)} /> : <Home />}
    </Shell>
  );
}
