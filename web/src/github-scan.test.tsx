import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import App from "./App";
import { ThemeProvider } from "./theme";

const ID = "SRA-0a1b2c";
const SHA = "0123456789ab";
const assessment = {
  id: ID, application: { name: "Orders API", cmdb_id: "" }, assessment_date: "2026-11-01", assessor: "a.person",
  profile: {}, templates: ["SRE-AT-11"], results: {}, rubric_direct: {}, findings: [],
  signoffs: [], detector_runs: {}, status: "draft",
};
const scanned = [
  { id: "EV-11111111", name: `github.com/acme/orders@${SHA}/.github/workflows/ci.yml`, kind: "ci_config", doc_type: null, meta: { tool: "github_actions" }, source: "github" },
  { id: "EV-22222222", name: `github.com/acme/orders@${SHA}/github-dependabot-alerts.cdx.json`, kind: "scan", doc_type: null, meta: { format: "cyclonedx", findings: 2 }, source: "github" },
];

const signedIn = { host: "github.com", signed_in: true, account: "octocat", source: "gh", scopes: ["repo"], env_token: null,
  interactive_login: true, token_login: false, login: null };
const signedOut = { ...signedIn, signed_in: false, account: null, source: null, scopes: [] };

function mockApi(status: { available: boolean; error: string | null; hosts: string[]; default_host: string }, scanStatus = 201,
  start: Record<string, unknown> = signedIn) {
  let files: unknown[] = [];
  let auth: Record<string, unknown> = { ...start };
  let polls = 0;
  let job: Record<string, unknown> = { state: "idle" };
  const json = (body: unknown, code = 200) => Promise.resolve(new Response(JSON.stringify(body), { status: code, headers: { "Content-Type": "application/json" } }));
  return vi.spyOn(window, "fetch").mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (url === "/api/llm/health") return json({ enabled: false, model: null, reachable: false });
    if (url === "/api/assessments") return json([]);
    if (url === `/api/assessments/${ID}`) return json({ assessment, scorecard: null, job });
    if (url === `/api/assessments/${ID}/job`) {
      // the scan job reports one phase, then finishes on the next poll and its evidence appears
      if (job.state === "running") {
        files = scanned;
        job = { ...job, state: "done", phase: "Finished", finished_at: "2026-11-14T10:01:00Z", summary: { scan: {
          repository: "acme/orders", host: "github.com", ref: "main", sha: `${SHA}aaaa`, files: { ci: 1, alerts: 1 },
          alerts: { dependabot: 2, code_scanning: null }, notes: [], skipped: 0, evidence_added: 2, fetch: "files",
          code: { files: 12, signals: 5, by_category: { latency: 3, security: 2 }, capabilities: ["timeouts"] } } } };
      }
      return json(job);
    }
    if (url === `/api/assessments/${ID}/candidates`) return json({ profile: {}, candidates: [] });
    if (url === "/api/catalog/templates/SRE-AT-11") return json({ id: "SRE-AT-11", title: "Vulnerabilities", domain: "SRE", variant: "", priority: "P1", applicability: "", purpose: "", criteria: [] });
    if (url === "/api/github/status") return json(status);
    if (url.startsWith("/api/github/auth")) {
      // the device sign-in completes on the second poll
      if ((auth.login as any)?.state === "pending" && ++polls >= 2) auth = { ...signedIn, login: { ...(auth.login as any), state: "done", account: "octocat" } };
      return json(auth);
    }
    if (url === "/api/github/login" && method === "POST") {
      const login = { host: "github.com", state: "pending", code: "AB12-CD34", url: "https://github.com/login/device", error: null, account: null, expires_in: 899 };
      auth = { ...auth, login };
      return json(login, 202);
    }
    if (url === "/api/github/logout" && method === "POST") { auth = { ...signedOut }; return json(auth); }
    if (url === `/api/assessments/${ID}/evidence`) {
      return json({ files, errors: [], summary: {}, synced: [], notes: [],
        repositories: files.length ? [{ label: `github.com/acme/orders@${SHA}`, host: "github.com", repository: "acme/orders", sha: SHA, files: files.length }] : [] });
    }
    if (url === `/api/assessments/${ID}/github` && method === "POST") {
      if (scanStatus !== 201) return json({ detail: "gh is not signed in to github.com. Run `gh auth login --hostname github.com`" }, scanStatus);
      job = { state: "running", phase: "Reading files", step: 96, total: 140, current: "deploy/values.yaml",
        started_at: new Date().toISOString(), updated_at: new Date().toISOString(), finished_at: null, warnings: 0,
        log: [{ t: new Date().toISOString(), level: "info", msg: "Scan of github.com/acme/orders@main queued by a.person" }],
        job: { job: "github_scan", repository: "acme/orders", ref: "main", host: "github.com" } };
      return json(job, 202);
    }
    return json({ detail: `unmocked ${method} ${url}` }, 404);
  });
}

async function openEvidence() {
  window.sessionStorage.setItem(`tab.${ID}`, "evidence");
  window.location.hash = `#/a/${ID}`;
  render(<ThemeProvider><App /></ThemeProvider>);
  return screen.findByRole("heading", { level: 2, name: "Scan a GitHub repository" });
}

describe("GitHub repository scan", () => {
  it("scans owner/name with a ref and lists the scanned files as GitHub evidence", async () => {
    const fetch = mockApi({ available: true, error: null, hosts: ["github.com"], default_host: "github.com" });
    await openEvidence();
    const button = screen.getByRole("button", { name: "Scan repository" });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Repository"), { target: { value: "acme/orders" } });
    fireEvent.change(screen.getByLabelText(/Branch, tag or commit/), { target: { value: "main" } });
    expect(screen.getByText("Host: github.com")).toBeInTheDocument();
    await act(async () => { fireEvent.click(button); });
    // the scan runs as a job: its progress shows in the status panel, with a cancel button
    const panel = await screen.findByRole("status", { name: "Repository scan status" });
    expect(within(panel).getByText("Scanning acme/orders · Reading files")).toBeInTheDocument();
    expect(within(panel).getByText("96 of 140 files done (69%)")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Cancel scan" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Busy…" })).toBeDisabled();
    expect(await screen.findByText("Repository scan finished", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByText(/Scanned acme\/orders@0123456789ab \(main\): 2 evidence files added; open alerts: Dependabot 2, code scanning n\/a; source code: 12 files analysed, 5 signals \(latency 3, security 2\); read file by file/)).toBeInTheDocument();
    const call = fetch.mock.calls.find(([u]) => String(u) === `/api/assessments/${ID}/github`)!;
    expect(JSON.parse(String(call[1]!.body))).toEqual({ repository: "acme/orders", ref: "main", host: "github.com", alerts: true, signals: true, code: true });
    await waitFor(() => expect(screen.getByText(`github.com/acme/orders@${SHA}/.github/workflows/ci.yml`)).toBeInTheDocument());
    expect(screen.getByText("ci_config")).toBeInTheDocument();
    expect(screen.getAllByText("GitHub").length).toBe(2);
    expect(screen.getByText(/Scanned from GitHub: github.com\/acme\/orders@0123456789ab \(2 files\)/)).toBeInTheDocument();
  }, 10000);

  it("shows why a scan failed, and offers a host choice when several are allowed", async () => {
    mockApi({ available: true, error: null, hosts: ["github.com", "ghe.corp.example"], default_host: "github.com" }, 503);
    await openEvidence();
    expect(screen.getByLabelText("Host")).toHaveValue("github.com");
    fireEvent.change(screen.getByLabelText("Repository"), { target: { value: "acme/orders" } });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Scan repository" })); });
    expect(await screen.findByText(/503: gh is not signed in to github.com/)).toBeInTheDocument();
  });

  it("warns and disables the scan when gh is not installed on the server", async () => {
    mockApi({ available: false, error: "The GitHub CLI (gh) is not installed on this server.", hosts: ["github.com"], default_host: "github.com" });
    await openEvidence();
    expect(await screen.findByText(/gh\) is not installed on this server/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Repository"), { target: { value: "acme/orders" } });
    expect(screen.getByRole("button", { name: "Scan repository" })).toBeDisabled();
  });
});

describe("GitHub sign-in", () => {
  it("shows the connection and signs out after confirming", async () => {
    const fetch = mockApi({ available: true, error: null, hosts: ["github.com"], default_host: "github.com" });
    await openEvidence();
    expect(await screen.findByText("Signed in")).toBeInTheDocument();
    expect(screen.getByText("octocat")).toBeInTheDocument();
    expect(screen.getByText(/gh sign-in on this server/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    const dialog = await screen.findByRole("dialog", { name: "Sign out of GitHub?" });
    await act(async () => { fireEvent.click(within(dialog).getByRole("button", { name: "Sign out" })); });
    expect(await screen.findByText("Not signed in")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith("/api/github/logout", expect.objectContaining({ method: "POST" }));
    fireEvent.change(screen.getByLabelText("Repository"), { target: { value: "acme/orders" } });
    expect(screen.getByRole("button", { name: "Scan repository" })).toBeDisabled();
  });

  it("signs in with the device flow: shows the one-time code and link, then polls until gh is signed in", async () => {
    mockApi({ available: true, error: null, hosts: ["github.com"], default_host: "github.com" }, 201, signedOut);
    await openEvidence();
    expect(await screen.findByText("Not signed in")).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Sign in to GitHub" })); });
    const dialog = await screen.findByRole("dialog", { name: "Sign in to github.com" });
    expect(within(dialog).getByLabelText("One-time code")).toHaveTextContent("AB12-CD34");
    expect(within(dialog).getByRole("link", { name: "Open github.com/login/device" })).toHaveAttribute("href", "https://github.com/login/device");
    expect(within(dialog).queryByLabelText("Token")).not.toBeInTheDocument();      // token sign-in is opt-in
    expect(await screen.findByText("Signed in to github.com as octocat.", {}, { timeout: 6000 })).toBeInTheDocument();
    expect(screen.getByText("Signed in")).toBeInTheDocument();
  }, 10000);

  it("explains how to connect when interactive sign-in is off (shared server)", async () => {
    mockApi({ available: true, error: null, hosts: ["github.com"], default_host: "github.com" }, 201, { ...signedOut, interactive_login: false });
    await openEvidence();
    expect(await screen.findByText(/An administrator sets GH_TOKEN from the/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign in to GitHub" })).not.toBeInTheDocument();
  });
});
