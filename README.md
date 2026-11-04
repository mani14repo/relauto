# Site Reliability Assessment Bot (SRA)

Helps an SRE run a reliability assessment end to end, using the FSI REO templates in `docs/OIPReqs`:

1. **Selects the templates** that apply to the application (39 templates: SRE-AT-01..16, ITSM-AT-01..12, CMDB-AT-01..11).
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
| Catalog | `sra.catalog` | Parses both template layouts (Variant A: 32 templates, 10-row tables; Variant B: 7 templates, 13-row tables), the rubric, the Evidence Catalog and the playbooks into `catalog/*.yaml`. Totals: 189 criteria, 18 rubric criteria, 35 playbooks |
| Crosswalk | `config/crosswalk.yaml` | Maps template criterion → rubric P1/P2 → standard → playbook. No OIPReqs document provides this mapping, so it is curated here (DRAFT) |
| Selection | `sra.selection` + `config/selection_rules.yaml` | Applicability predicates per template, scored and explained; the SRE confirms |
| Evidence | `sra.evidence` | ServiceNow CSV and Table API JSON (incident, change_request, problem, kb_knowledge, em_alert, cmdb_ci, cmdb_rel_ci); docx/pdf/md; Trivy/Grype/CycloneDX/SARIF; Kubernetes YAML; Jenkinsfile/GitHub Actions |
| Detectors | `sra.detectors` | `rubric_rules` (all 18 Evidence Catalog rules), `incident_anomalies`, `vulnerabilities`, `config_checks`. Thresholds are in `config/detectors.yaml` |
| LLM | `sra.llm`, `sra.retrieval` | BM25, plus optional local ONNX embeddings fused with RRF. Per-criterion JSON-schema calls; `verify.py` rejects invented citations |
| Scoring | `sra.scoring` + `config/scoring_matrix.yaml` | See below |
| Reports | `sra.report` | `docx_fill.py` edits the original template XML in place; `summary.py` produces the rubric Section 8 output |
| Service | `sra.service`, `sra.api`, `sra.worker`, `sra.store` | FileStore for dev; PgStore with a transactional outbox → Kafka |

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
- **Tier thresholds:** service tier sets the P2-07 DR-test interval and the P2-10 CMDB completeness threshold.
- **Threshold conflicts:** where a template and the rubric disagree (runbook review within 6 vs 12 months), the template threshold decides the template criterion, the rubric threshold decides the rating, and the report flags the conflict.

## Quick start (local)

```bash
uv venv --python 3.13 .venv && uv pip install --python .venv -e ".[dev]"
.venv/Scripts/python -m sra.cli catalog                       # compile docs/OIPReqs -> catalog/
.venv/Scripts/python eval/synthetic/make_scenario.py          # synthetic app with seeded defects
.venv/Scripts/python -m sra.cli assess eval/scenarios/orders_batch --as-of 2026-11-04T09:00 --out out/demo
.venv/Scripts/python -m pytest -q
```

UI: `cd web && npm install && npm run build`, then run `python -m sra.cli serve` and open http://localhost:8080.

Local LLM: install Ollama and run `ollama pull qwen2.5:7b-instruct` (or set `LLM_MODEL`). Then add `--llm` to `assess`, or tick "Use local LLM" in the UI. To compare models, use `python eval/run_eval.py --llm --model <a> --model <b>`.

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
