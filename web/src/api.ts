import type { Job } from "./JobPanel";
export type Result = "Met" | "Gap" | "Insufficient Evidence" | "Not Applicable";
export type Risk = "Critical" | "High" | "Medium" | "Low";

export interface CriterionResult {
  criterion_id: string;
  result: Result | null;
  risk: Risk | null;
  rationale: string;
  evidence_refs: string[];
  source: "rule" | "llm" | "human" | "derived" | null;
  confidence: number | null;
  needs_human_review: boolean;
  na_justification: string;
  na_approved: boolean;
  reviewer: string;
  recommendation: string;
  measured_value: string;          // V2 CMDB criteria
  maturity_level: number | null;   // V2 CMDB criteria, L0-L4
}

export interface Recommendation {
  action: string;
  playbooks: string[];
  playbook_steps: string[];
  owner: string;
  due_date: string | null;
  source: string;
}

export interface Finding {
  id: string;
  type: "gap" | "vulnerability" | "anomaly" | "observation";
  severity: Risk;
  title: string;
  detail: string;
  criterion_ids: string[];
  rubric_ids: string[];
  evidence_refs: string[];
  detector: string;
  confidence: number;
  status: "open" | "accepted" | "disputed" | "resolved";
  recommendation: Recommendation | null;
}

export interface Assessment {
  id: string;
  application: { name: string; cmdb_id: string };
  assessment_date: string;
  assessor: string;
  profile: Record<string, unknown>;
  templates: string[];
  results: Record<string, CriterionResult>;
  rubric_direct: Record<string, CriterionResult>;
  findings: Finding[];
  signoffs: { role: string; name: string; date: string | null; decision: string }[];
  detector_runs: Record<string, string>;
  status: string;
}

export interface TemplateScore {
  template_id: string; domain: string; score: number | null; weight: number;
  met: number; gaps: number; insufficient: number; na: number; unevaluated: number;
}

export interface ScoreCard {
  matrix_version: string;
  template_scores: TemplateScore[];
  domain_scores: Record<string, number>;
  index: number | null;
  maturity_level: number | null;
  maturity_name: string | null;
  maturity_capped: boolean;
  rating: string;
  rating_outcome: string;
  rubric: { rubric_id: string; priority: string; domain: string; name: string; result: Result; contributing: string[] }[];
  p1_gaps: string[];
  p2_gaps: string[];
  evidence_coverage_pct: number;
  conflicts: string[];
  cmdb: CmdbReadiness | null;
}

export interface CmdbReadiness {
  cri: number | null; complete: boolean; assessed: number; total: number; level: number | null; level_name: string | null;
  dimensions: { name: string; weight: number; score: number | null; assessed: number; total: number; missing: string[] }[];
  gates: { id: string; name: string; unlocks: string; status: "passed" | "not passed" | "undetermined"; failed: string[]; pending: string[] }[];
  thresholds_status: string;
}

export interface Criterion {
  id: string; number: number; name: string; standard_ref: string; applicability: string; required_evidence: string;
  evaluation_method: string; methods: string[]; acceptance_criteria: string; default_risk: Risk; accountable_owner: string;
  // V2 (CMDB/CSDM) templates only
  rubric_refs?: string[]; rubric_enablers?: string[]; formula?: string; maturity_bands?: string; data_source?: string;
  look_for?: string; csdm_layer?: string; playbooks?: string[];
}

export interface Template {
  id: string; title: string; domain: string; variant: string; priority: string; applicability: string; purpose: string;
  criteria: Criterion[];
}

export interface Candidate { template_id: string; title: string; selected: boolean; always: boolean; score: number | null; reasons: string[] }

const user = () => localStorageSafe("sra.user") || "";

function localStorageSafe(k: string): string | null {
  try { return window.localStorage.getItem(k); } catch { return null; }
}
export function setUser(v: string) {
  try { window.localStorage.setItem("sra.user", v); } catch { /* ignore */ }
}
export function getUser() { return user(); }

async function req<T>(method: string, url: string, body?: unknown, isForm = false): Promise<T> {
  const headers: Record<string, string> = {};
  if (user()) headers["X-User"] = user();
  if (body !== undefined && !isForm) headers["Content-Type"] = "application/json";
  const r = await fetch(url, { method, headers, body: isForm ? (body as FormData) : body !== undefined ? JSON.stringify(body) : undefined });
  if (!r.ok) {
    let detail = r.statusText;
    try { const j = await r.json(); detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* */ }
    throw new Error(`${r.status}: ${detail}`);
  }
  return r.json() as Promise<T>;
}

export const api = {
  list: () => req<any[]>("GET", "/api/assessments"),
  templates: () => req<any[]>("GET", "/api/catalog/templates"),
  template: (id: string) => req<Template>("GET", `/api/catalog/templates/${id}`),
  parseProfile: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return req<{ profile: Record<string, unknown>; ignored: string[]; filename: string }>("POST", "/api/profile/parse", fd, true);
  },
  create: (profile: Record<string, unknown>) => req<{ assessment: Assessment; candidates: Candidate[] }>("POST", "/api/assessments", { profile }),
  get: (id: string) => req<{ assessment: Assessment; scorecard: ScoreCard | null; job: Job }>("GET", `/api/assessments/${id}`),
  candidates: (id: string) => req<{ profile: any; candidates: Candidate[] }>("GET", `/api/assessments/${id}/candidates`),
  setTemplates: (id: string, templates: string[]) => req<Assessment>("PUT", `/api/assessments/${id}/templates`, { templates }),
  patchProfile: (id: string, patch: Record<string, unknown>) => req<Assessment>("PATCH", `/api/assessments/${id}/profile`, patch),
  edit: (id: string, patch: { profile?: Record<string, unknown>; assessment_date?: string; assessor?: string }) =>
    req<{ assessment: Assessment; rerun_needed: boolean }>("PATCH", `/api/assessments/${id}`, patch),
  remove: (id: string) => req<{ id: string; application: string; evidence_files: number }>("DELETE", `/api/assessments/${id}`),
  upload: (id: string, files: FileList | File[], hint?: string) => {
    const fd = new FormData();
    Array.from(files).forEach((f) => fd.append("files", f, (f as any).webkitRelativePath || f.name));
    if (hint) fd.append("hint", hint);
    return req<any[]>("POST", `/api/assessments/${id}/evidence`, fd, true);
  },
  evidence: (id: string) => req<{ files: any[]; errors: string[]; summary: Record<string, number>; synced?: { table: string; rows: number; source: string }[]; notes?: string[] }>("GET", `/api/assessments/${id}/evidence`),
  run: (id: string, llm: boolean) => req<Job>("POST", `/api/assessments/${id}/run`, { llm }),
  job: (id: string) => req<Job>("GET", `/api/assessments/${id}/job`),
  cancel: (id: string) => req<Job>("POST", `/api/assessments/${id}/cancel`),
  patchCriterion: (id: string, cid: string, patch: Record<string, unknown>) => req<any>("PATCH", `/api/assessments/${id}/criteria/${cid}`, patch),
  confirm: (id: string, cid: string) => req<any>("POST", `/api/assessments/${id}/criteria/${cid}/confirm`),
  chat: (id: string, cid: string, question: string) => req<{ answer: string }>("POST", `/api/assessments/${id}/criteria/${cid}/chat`, { question }),
  patchFinding: (id: string, fid: string, patch: Record<string, unknown>) => req<Finding>("PATCH", `/api/assessments/${id}/findings/${fid}`, patch),
  gate: (id: string, final = false) => req<{ ok: boolean; blockers: string[]; warnings: string[] }>("GET", `/api/assessments/${id}/gate?final=${final}`),
  signoff: (id: string, role: string, name: string, decision: string) => req<Assessment>("POST", `/api/assessments/${id}/signoffs`, { role, name, decision }),
  audit: (id: string) => req<any[]>("GET", `/api/assessments/${id}/audit`),
  llmHealth: () => req<{ enabled: boolean; model: string | null; reachable: boolean }>("GET", "/api/llm/health"),
  reportUrl: (id: string) => `/api/assessments/${id}/report`,
};
