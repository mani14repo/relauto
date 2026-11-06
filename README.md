# Site Reliability Assessment Bot (SRA)

Helps an SRE run a reliability assessment end to end, using the FSI REO templates in `docs/OIPReqs`:

1. **Selects the templates** that apply to the application (41 templates: SRE-AT-01..16, ITSM-AT-01..12, CMDB-AT-01..13 — the CMDB set is the V2 CMDB/CSDM release).
2. **Ingests evidence**: ServiceNow exports, documents, scanner output, Kubernetes manifests and CI configuration. Secrets are redacted on the way in.
3. **Detects gaps, vulnerabilities and anomalies** with deterministic detectors first. A **local LLM** (Ollama, CPU) then evaluates only the criteria the rules cannot settle, and every AI result is checked against the evidence it cites.
4. **Recommends a fix for every finding**, grounded in the numbered steps of the OIPReqs playbooks, with an owner and a due date.
5. **Scores** each criterion, template and domain. Applies the rubric's gating Readiness Rating (P1/P2 gap counts), and computes a Reliability Readiness Index and a maturity level (L1–L5).
6. **Writes the filled-in `.docx`** for each template in its original format, plus an Assessment Summary report.

The SRE reviews and overrides everything in a guided web UI. Human decisions are kept across re-runs and logged as feedback for later fine-tuning.

## How it fits together

```
React UI ──► sra-api (FastAPI :8080) ──► Postgres (Cloud SQL + pgvector)  ◄── cloud-sql-proxy sidecar (IAM)
                 │ outbox ─► Kafka (sra.assessment.events, sra.jobs) ─► sra-worker ──► Ollama (CPU, local model)
                 └── GCS (evidence blobs, reports)
```

| Stage | Module | Notes |
|---|---|---|
| Catalog | `sra.catalog` | Parses three template layouts (Variant A: 23 SRE/ITSM templates, 10-row tables; Variant B: 5, 13-row tables; Variant C: the 13 V2 CMDB/CSDM templates, with criterion IDs, rubric links, formulas and L0–L4 maturity bands), the rubric, the Evidence Catalog and both playbook layouts into `catalog/*.yaml`. When a folder holds two versions of a document, the highest version wins. Totals: 215 criteria, 18 rubric criteria, 37 playbooks |
| Crosswalk | `config/crosswalk.yaml` + V2 templates | SRE/ITSM: template criterion → rubric P1/P2 → standard → playbook, curated here (DRAFT). CMDB: read from the V2 templates themselves; criteria marked “(enabler)” support a rubric criterion without deciding it |
| Selection | `sra.selection` + `config/selection_rules.yaml` | Applicability predicates per template, scored and explained; the SRE confirms |
| Evidence | `sra.evidence` | ServiceNow CSV and Table API JSON (incident, change_request, problem, kb_knowledge, em_alert, cmdb_ci, cmdb_rel_ci); docx/pdf/md; Trivy/Grype/CycloneDX/SARIF; Kubernetes YAML; Jenkinsfile/GitHub Actions |
| Detectors | `sra.detectors` | `rubric_rules` (all 18 Evidence Catalog rules), `criteria_rules` (25 template criteria computed from ingested data), `checklists` (37 document checklists, `config/checklists.yaml`), `incident_anomalies`, `incident_clusters`, `vulnerabilities`, `config_checks`. Thresholds are in `config/detectors.yaml` |
| LLM | `sra.llm`, `sra.retrieval` | BM25, plus optional local ONNX embeddings fused with RRF. Per-criterion JSON-schema calls; `verify.py` rejects invented citations |
| Scoring | `sra.scoring` + `config/scoring_matrix.yaml` | See below |
| Reports | `sra.report` | `docx_fill.py` edits the original template XML in place; `summary.py` produces the rubric Section 8 output |
| Service | `sra.service`, `sra.api`, `sra.worker`, `sra.store` | FileStore for dev; PgStore with a transactional outbox → Kafka |

### Template criteria computed from data (`criteria_rules`)

These are 25 criteria that used to go to the LLM, now settled by plain rules over data the app already ingests: ServiceNow incidents, changes, problems, KB articles, alerts and CMDB records, Kubernetes manifests, and CI configuration. Each rule sets a result only when the data it needs is present; otherwise the criterion still goes to the LLM or the assessor. When part of a criterion can't be seen in data (a test record, an approval), the result is flagged for human review. V2 CMDB criteria also get a measured value and a maturity level.

Several rules need fields to be added to existing exports (`time_worked`, `reassignment_count`, `rfc`, `last_discovered`, a KB review owner). Field names and label keys marked CONFIRM under `criteria_rules` in `config/detectors.yaml` must be checked against the instance. The label keys come from CMDB-PB-10 and are pending OD-4.

### Document checklists (`checklists`)

These are 37 criteria that ask whether the evidence contains specific things: RTO/RPO values and a recent DR test, a toil inventory with weekly hours, a RACI with no blank cells, a KPI catalogue naming the five KPIs, and so on. Each has a checklist in `config/checklists.yaml`. The checklist says which documents to look in (by file name or title, or by document type), which items must be present (keyword patterns, minimum counts, things that must not appear), and optionally freshness or a monthly cadence. Two criteria apply their checklist to each record (every P1 incident's notes; each KB article). Three are small dedicated checks:
- RTO/RPO in the documents against the CMDB and the tier limits;
- a continuity review dated after the latest P1/P2;
- a KB search for the top incident categories.

How a result is decided:
- **No matching document:** nothing is set, and the criterion goes to the assessor.
- **Every item found:** Met. A few checklists still ask for review because they need an approval or count the documents can't show.
- **Anything missing:** Gap, naming what's missing, and always flagged for review. Wording the patterns don't expect can hide an item that's really there.

Matching by meaning (embeddings) is built but turned off: `bge-small` scored unrelated document lines as high as real paraphrases. The checklists are a DRAFT written from the template text, so SREs should review the patterns.

`docs/llm-replacement-inventory.md` sorts every criterion the LLM covers by what could replace it.

### Recurring incidents by meaning (`incident_clusters`, machine learning, advisory)

The rules find recurring incidents by category/subcategory (P2-02) and by identical wording (ITSM-AT-03-C01). The same root cause is often filed under different categories and words, so `incident_clusters` also groups incidents by what they say:

1. Takes the short description (and description) of each incident in the last 90 days. It removes numbers, URLs and the application's own name ("orders-api", "Orders"), since every incident mentions it and that makes unrelated incidents look alike.
2. Turns each distinct text into a vector with the local embedding model (`bge-small` via fastembed, CPU). Without fastembed it falls back to TF-IDF, which only groups incidents that share words. The status line says which one ran.
3. Groups texts with leader (Butina) clustering. Two texts count as similar when they are above a fixed floor **and** clearly above how alike this application's incidents usually are.
4. Reports a group only when the rules cannot already see it:
   - at least 3 incidents and 3 different descriptions;
   - on more than one day;
   - none of them linked to a Problem.

Findings are advisory (Medium, confidence below 1). They never set a criterion result or change the rating; ITSM-AT-03-C01 is flagged for human review instead. To tune or turn it off, edit `incident_clusters` in `config/detectors.yaml`. The thresholds were tuned on a small hand-made set of reworded incidents and on the 10 eval scenarios (no false alarms there), not on real ServiceNow data. Raise `similarity.embedding` or `margin_over_typical` if groups mix unrelated issues. The scenario kit has a `hidden_recurrence` defect for this detector.

## Scoring matrix (`config/scoring_matrix.yaml`, DRAFT, pending REO/SRE approval)

The rubric defines a gating rating and domain weights but **no numeric score**. The matrix adds one.

- **Criterion points:** Met = 1, Gap = 0. Insufficient Evidence = 0 (per rubric §7). Not Applicable is excluded only when approved; unapproved N/A, and any criterion not yet evaluated, score as Insufficient Evidence.
- **Weights:** each criterion is weighted by risk (Critical 4 / High 3 / Medium 2 / Low 1). Templates are weighted by priority (P1 3 / P2 2 / P3 1).
- **Readiness Index:** 0.5·SRE + 0.3·ITSM + 0.2·CMDB. These are the rubric's weights, renormalised over the domains actually assessed.
- **Maturity:** L1 Initial < 40 ≤ L2 Developing < 60 ≤ L3 Defined < 75 ≤ L4 Managed < 90 ≤ L5 Optimized.
- **Readiness Rating** (exactly as the rubric defines it):

  | Rating | Condition |
  |---|---|
  | Not Ready | any P1 gap |
  | At Risk | 0 P1 gaps and ≥ 4 P2 gaps |
  | Conditional | 0 P1 gaps and 1–3 P2 gaps |
  | Ready | no gaps |

  A rubric criterion's result is the worst of its direct rule result and every mapped template criterion.
- **Caps:** a Not Ready rating caps maturity at L2; At Risk caps it at L3.
- **Tier thresholds:** service tier sets the P2-07 DR-test interval and the P2-10 CMDB completeness threshold. V2 CMDB criteria use the CMDB-STD-00 thresholds TH-01…TH-14 (`cmdb_v2.thresholds`, pending OD-2/OD-3).
- **CMDB / CSDM readiness (V2, CMDB-AT-13):** the CMDB Readiness Index (six weighted dimensions over criterion maturity levels L0–L4), an indicative CMDB maturity, and reporting Gates A–D (what may be reported as authoritative). Shown beside the rating; it never changes it.
- **Threshold conflicts:** where a template and the rubric disagree (runbook review within 6 vs 12 months), the template threshold decides the template criterion, the rubric threshold decides the rating, and the report flags the conflict.

## Quick start (local)

```bash
uv venv --python 3.13 .venv && uv pip install --python .venv -e ".[dev]"
.venv/Scripts/python -m sra.cli catalog                       # compile docs/OIPReqs -> catalog/
.venv/Scripts/python eval/synthetic/make_scenario.py          # synthetic app with seeded defects (files to upload are in evidence/)
.venv/Scripts/python -m sra.cli assess eval/scenarios/orders_batch --as-of 2026-11-04T09:00 --out out/demo
.venv/Scripts/python -m pytest -q
```

More test apps: `python eval/synthetic/generate_scenarios.py` asks a Foundry model (`FOUNDRY_*`, `APP_MODEL` in `.env`) to design four varied applications. You can also pass your own `--brief "..."`. The model only picks names, a profile and which defects to plant, from the fixed list in `eval/synthetic/scenario_kit.py`. Code renders the evidence and derives the answer key from that list, so the key doesn't depend on the model being right. Each app goes to `eval/scenarios/<slug>/`: `evidence/` (upload these), `profile.yaml` (form answers), `expected.yaml` (answer key) and `spec.yaml` (`--from-spec` re-renders it without calling the model). `python eval/run_eval.py --all` checks every scenario.

Settings: everything is optional. Copy `.env.example` to `.env` to change the model, or to switch storage to Postgres and jobs to Kafka.

Runs: the assessment page shows live progress for an automated pass and has a **Cancel run** button. Cancelling stops the run at its next step, or within a second during a model call (the request is streamed, and closing it makes Ollama stop generating). Nothing from a cancelled run is saved, so the assessment keeps its previous results. The API equivalent is `POST /api/assessments/{id}/cancel`; it works for the in-process runner and the Kafka worker alike.

UI: `python -m sra.cli serve --build` builds the web UI (running `npm install` the first time) and then starts the API; open http://localhost:8080. Without `--build` it serves the existing `web/dist`.

Local LLM: install Ollama and run `ollama pull qwen3.5:4b` (the laptop default; set `LLM_MODEL` to change it). Thinking mode is off by default (`LLM_THINK=false`), because reasoning tokens are too slow on CPU. For the cluster, the candidate is `gemma4:26b`, a mixture-of-experts model that uses about 4B parameters per token; confirm it with the eval. Then add `--llm` to `assess`, or tick "Use local LLM" in the UI. To compare models, use `python eval/run_eval.py --llm --model <a> --model <b>`.

Full stack: `docker compose up` (Postgres+pgvector, Kafka KRaft, Ollama, api, worker).

## Deploying to GKE (CAP)

`deploy/helm/sra`:
- the API (2 replicas, PDB, probes, non-root, read-only root filesystem) with a native-sidecar cloud-sql-proxy using IAM auth;
- the Kafka worker;
- an Ollama StatefulSet on a CPU high-memory node pool. Model weights are mirrored to GCS and copied into the PVC by an init container, because the cluster has no internet access.

The image builds through `.github/workflows/ci.yml`, using the shared `python_docker.yaml` workflow. All base images come from JFrog.

## Open items (need stakeholder confirmation)

- The rubric, the scoring weights and the maturity bands are V1 drafts.
- The crosswalk is curated by hand.
- `[CONFIRM]` ServiceNow tables and fields in `config/detectors.yaml` (e.g. `u_slo_definition`, `u_business_application`).
- Mirroring model weights through JFrog/GCS; pgvector on the Cloud SQL instance; Kafka topics and auth; UI ingress/IAP.
- Duplicate playbooks: SRE-PB-11/14 and SRE-PB-13/15 (the crosswalk uses the lower number).
