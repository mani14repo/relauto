import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import App from "./App";
import { THEME_KEY, ThemeProvider } from "./theme";

const ID = "SRA-0a1b2c";
const assessment = {
  id: ID, application: { name: "Orders API", cmdb_id: "" }, assessment_date: "2026-11-01", assessor: "a.person",
  profile: {}, templates: ["SRE-AT-01"], results: {}, rubric_direct: {}, findings: [],
  signoffs: [{ role: "SRE lead", name: "", date: null, decision: "" }], detector_runs: {}, status: "draft",
};
const scorecard = {
  matrix_version: "v1", template_scores: [], domain_scores: { SRE: 62 }, index: 62, maturity_level: 2, maturity_name: "Managed",
  maturity_capped: false, rating: "Conditional", rating_outcome: "Ready with conditions", rubric: [], p1_gaps: [], p2_gaps: [],
  evidence_coverage_pct: 40, conflicts: [], cmdb: null,
};
const template = { id: "SRE-AT-01", title: "Observability", domain: "SRE", variant: "", priority: "P1", applicability: "", purpose: "", criteria: [] };

/** Stub fetch with the API routes the pages call; anything else is a 404 so a new call shows up as a failure. */
function mockApi(rows: unknown[] = [], over: { assessment?: object; template?: object } = {}) {
  const a = { ...assessment, ...over.assessment };
  const t = { ...template, ...over.template };
  const json = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
  return vi.spyOn(window, "fetch").mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (url === "/api/llm/health") return json({ enabled: true, model: "llama3", reachable: true });
    if (url === "/api/assessments" && method === "GET") return json(rows);
    if (url === `/api/assessments/${ID}` && method === "GET") return json({ assessment: a, scorecard, job: { state: "idle" } });
    if (url === `/api/assessments/${ID}` && method === "DELETE") return json({ id: ID, application: "Orders API", evidence_files: 0 });
    if (url === `/api/assessments/${ID}/candidates`) return json({ profile: {}, candidates: [{ template_id: "SRE-AT-01", title: "Observability", selected: true, always: true, score: null, reasons: [] }] });
    if (url === "/api/catalog/templates/SRE-AT-01") return json(t);
    if (url.startsWith(`/api/assessments/${ID}/gate`)) return json({ ok: false, blockers: ["SRE-AT-01-C01 not evaluated"], warnings: [] });
    return json({ detail: `unmocked ${method} ${url}` }, 404);
  });
}

const renderApp = () => render(<ThemeProvider><App /></ThemeProvider>);

describe("shell", () => {
  it("renders the OIP frame: side nav under RELIABILITY (SRE), header status, assessments page", async () => {
    mockApi();
    renderApp();
    const nav = screen.getByRole("navigation", { name: "Side navigation" });
    expect(within(nav).getByText("Reliability (SRE)")).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: /Reliability assessments/ })).toHaveAttribute("href", "#/");
    expect(within(nav).getByRole("link", { name: /New assessment/ })).toHaveAttribute("href", "#/new");
    expect(screen.getByRole("heading", { level: 1, name: "Reliability assessments" })).toBeInTheDocument();
    expect(await screen.findByText(/No assessments yet/)).toBeInTheDocument();
    expect(await screen.findByText("LLM llama3: ready")).toBeInTheDocument();
    expect(screen.getAllByText(/^as of /).length).toBeGreaterThan(0);
  });

  it("defaults to the g100 dark theme and the side-nav footer switches to white, remembered in localStorage", async () => {
    mockApi();
    renderApp();
    const html = document.documentElement;
    await waitFor(() => expect(html).toHaveAttribute("data-theme", "dark"));
    expect(html).toHaveClass("cds--g100");
    fireEvent.click(screen.getByRole("button", { name: "Switch to the light theme" }));
    await waitFor(() => expect(html).toHaveAttribute("data-theme", "light"));
    expect(html).toHaveClass("cds--white");
    expect(html).not.toHaveClass("cds--g100");
    expect(window.localStorage.getItem(THEME_KEY)).toBe("light");
  });

  it("starts in the stored light theme", async () => {
    window.localStorage.setItem(THEME_KEY, "light");
    mockApi();
    renderApp();
    await waitFor(() => expect(document.documentElement).toHaveAttribute("data-theme", "light"));
    expect(screen.getByRole("button", { name: "Switch to the dark theme" })).toBeInTheDocument();
  });

  it("collapses the side nav to a rail and remembers it", () => {
    mockApi();
    const { container } = renderApp();
    const nav = screen.getByRole("navigation", { name: "Side navigation" });
    fireEvent.click(within(nav).getByRole("button", { name: "Collapse navigation" }));
    expect(container.querySelector(".sra-shell--rail")).not.toBeNull();
    expect(window.localStorage.getItem("sra-nav")).toBe("collapsed");
  });
});

describe("pages", () => {
  it("lists assessments and asks before deleting one", async () => {
    const fetch = mockApi([{ id: ID, application: "Orders API", date: "2026-11-01", status: "draft", templates: 3, findings: 2, rating: "At Risk", index: 48, maturity: "L1" }]);
    renderApp();
    expect(await screen.findByRole("link", { name: "Orders API" })).toHaveAttribute("href", `#/a/${ID}`);
    expect(screen.getByText("At Risk")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete Orders API" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Delete this assessment?")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(screen.queryByRole("link", { name: "Orders API" })).not.toBeInTheDocument());
    expect(fetch).toHaveBeenCalledWith(`/api/assessments/${ID}`, expect.objectContaining({ method: "DELETE" }));
  });

  it("shows the new-assessment form", async () => {
    mockApi();
    window.location.hash = "#/new";
    renderApp();
    expect(screen.getByRole("heading", { level: 1, name: "New assessment" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create and propose templates" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Application name/), { target: { value: "Orders API" } });
    expect(screen.getByRole("button", { name: "Create and propose templates" })).toBeEnabled();
  });

  it("opens an assessment with its steps as Carbon tabs and score tiles in Plex Mono with an as-of time", async () => {
    mockApi();
    window.location.hash = `#/a/${ID}`;
    renderApp();
    expect(await screen.findByRole("heading", { level: 1, name: "Orders API" })).toBeInTheDocument();
    // the open assessment gets a nav entry named after the application
    const nav = screen.getByRole("navigation", { name: "Side navigation" });
    expect(await within(nav).findByRole("link", { name: /Orders API/ })).toHaveAttribute("href", `#/a/${ID}`);
    expect(await screen.findByText("Proposed templates")).toBeInTheDocument();
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual(["1 · Templates", "2 · Evidence", "3 · Criteria", "4 · Findings", "5 · Score", "6 · Report & sign-off"]);
    await act(async () => { fireEvent.click(screen.getByRole("tab", { name: "5 · Score" })); });
    expect(await screen.findByText("Reliability Readiness Index")).toBeInTheDocument();
    const tile = screen.getByText("Reliability Readiness Index").closest(".kpi-tile")!;
    expect(tile.querySelector(".kpi")).toHaveTextContent("62");
    expect(within(tile as HTMLElement).getByText(/^as of /)).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByRole("tab", { name: "6 · Report & sign-off" })); });
    expect(await screen.findByText("1 blockers")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });

  it("lists criteria after a run and filters to the unevaluated ones from the notice", async () => {
    const criterion = {
      id: "SRE-AT-01-C01", number: 1, name: "Dashboards cover the golden signals", standard_ref: "", applicability: "",
      required_evidence: "Dashboards", evaluation_method: "Review", methods: [], acceptance_criteria: "All four signals", default_risk: "High", accountable_owner: "",
    };
    mockApi([], { assessment: { detector_runs: { rules: "2026-11-14T10:00:00Z" } }, template: { criteria: [criterion] } });
    window.sessionStorage.setItem(`tab.${ID}`, "criteria");
    window.location.hash = `#/a/${ID}`;
    renderApp();
    expect(await screen.findByText(/1 criteria have no automated result/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show them" }));
    expect(screen.getByRole("combobox", { name: "Show" })).toHaveValue("pending");
    expect(screen.getByRole("heading", { level: 2, name: /Dashboards cover the golden signals/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save decision" })).toBeEnabled();
  });
});
