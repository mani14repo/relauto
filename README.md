# Site Reliability Assessment Bot (SRA)

Helps an SRE run a reliability assessment end to end, using the FSI REO templates in `docs/OIPReqs`:

1. **Selects the templates** that apply to the application (41 templates: SRE-AT-01..16, ITSM-AT-01..12, CMDB-AT-01..13 — the CMDB set is the V2 CMDB/CSDM release).
2. **Ingests evidence**: ServiceNow exports, documents, scanner output, Kubernetes manifests and CI configuration. Secrets are redacted on the way in.
3. **Detects gaps, vulnerabilities and anomalies** with deterministic detectors first. An LLM (on CAP: Claude through the CVS LMS gateway, as OIP uses; locally: Ollama on CPU) then evaluates only the criteria the rules cannot settle, and every AI result is checked against the evidence it cites.
4. **Recommends a fix for every finding**, grounded in the numbered steps of the OIPReqs playbooks, with an owner and a due date.
5. **Scores** each criterion, template and domain. Applies the rubric's gating Readiness Rating (P1/P2 gap counts), and computes a Reliability Readiness Index and a maturity level (L1–L5).
6. **Writes the filled-in `.docx`** for each template in its original format, plus an Assessment Summary report.

The SRE reviews and overrides everything in a guided web UI. Human decisions are kept across re-runs and logged as feedback for later fine-tuning.

## How it fits together

```
React UI ──► sra (one container on CAP, :8080: UI, API, /healthz, in-process jobs and scheduler)
                 ├── cloud-sql-proxy sidecar (IAM) ──► CAP Postgres (sra_* tables beside OIP's cog_*, pgvector, evidence files)
                 └── LMS gateway (HTTPS) ──► AWS Bedrock ──► Claude          (local dev: Ollama on CPU)
Optional, off by default: Kafka (sra.jobs) + a separate worker, GCS for evidence files, an Ollama StatefulSet.
```

| Stage | Module | Notes |
|---|---|---|
| Catalog | `sra.catalog` | Parses three template layouts (Variant A: 23 SRE/ITSM templates, 10-row tables; Variant B: 5, 13-row tables; Variant C: the 13 V2 CMDB/CSDM templates, with criterion IDs, rubric links, formulas and L0–L4 maturity bands), the rubric, the Evidence Catalog and both playbook layouts into `catalog/*.yaml`. When a folder holds two versions of a document, the highest version wins. Totals: 215 criteria, 18 rubric criteria, 37 playbooks |
| Crosswalk | `config/crosswalk.yaml` + V2 templates | SRE/ITSM: template criterion → rubric P1/P2 → standard → playbook, curated here (DRAFT). CMDB: read from the V2 templates themselves; criteria marked “(enabler)” support a rubric criterion without deciding it |
| Selection | `sra.selection` + `config/selection_rules.yaml` | Applicability predicates per template, scored and explained; the SRE confirms |
| Evidence | `sra.evidence` | ServiceNow CSV, XLSX and Table API JSON (incident, change_request, problem, kb_knowledge, em_alert, cmdb_ci, cmdb_rel_ci); docx/pdf/md/txt; images (jpg/png/tiff/webp) and scanned PDFs via RapidOCR (CPU, weights inside the wheel); Trivy/Grype/CycloneDX/SARIF; Kubernetes YAML; Jenkinsfile/GitHub Actions |
| Detectors | `sra.detectors` | `rubric_rules` (all 18 Evidence Catalog rules), `criteria_rules` (25 template criteria computed from ingested data), `checklists` (37 document checklists, `config/checklists.yaml`), `data_rules` (52 criteria from extra ServiceNow tables and registers), `incident_anomalies`, `incident_clusters`, `vulnerabilities`, `config_checks`. Thresholds are in `config/detectors.yaml` |
| Retrieval | `sra.retrieval` | Hybrid: keyword + semantic vector search fused with RRF, optional local reranker. In PostgreSQL (tsvector + pgvector) when the service has a database, in memory otherwise |
| LLM | `sra.llm` | Per-criterion JSON-schema calls; `verify.py` rejects invented citations. On CAP, Claude through the LMS gateway in tool mode (forced `submit_answer` call, one corrective retry), as OIP does; locally, Ollama or any OpenAI-compatible server |
| Scoring | `sra.scoring` + `config/scoring_matrix.yaml` | See below |
| Reports | `sra.report` | `docx_fill.py` edits the original template XML in place; `summary.py` produces the rubric Section 8 output |
| Service | `sra.service`, `sra.api`, `sra.worker`, `sra.store` | FileStore for dev; PgStore (evidence files in `sra_blobs`; outbox → Kafka only when Kafka is configured); `sra.scheduler` runs ingestion in-process |

### Knowledge store and hybrid retrieval (`sra.knowledge`, `sra.connectors`, `sra.retrieval`)

Built to `docs/Hybrid Retrieval Architecture Overview.png`: one PostgreSQL database, no separate vector database.

**The store stands on its own.** It is not tied to assessments.

| Table | Holds |
|---|---|
| `sra_documents` | One row per source document or record: an uploaded evidence file, a ServiceNow record, an OIPReqs standard or playbook. Metadata (source system, type, application), source ids, access list, tags. |
| `sra_chunks` | Its text in retrievable pieces: `tsv` + GIN index for keyword search, `embedding vector(384)` + HNSW index for semantic search. |
| `sra_relations` | Links between documents (below). |
| `sra_sync_state` | Where each connector stream stopped. |

A file uploaded to an assessment belongs to it and is deleted with it. ServiceNow records and the standards have no owner: they stay, and every consumer can search them.

**Sources (diagram box 1).**

| Source | How it arrives | Status |
|---|---|---|
| Uploaded evidence | The upload API | Working |
| ServiceNow (incident, problem, change_request, kb_knowledge, em_alert, cmdb_ci, cmdb_rel_ci) | Connector: Table API, incremental by `sys_updated_on`, paged, retried (`sra.connectors.servicenow`, `config/sources.yaml`) | Built and tested against a stand-in API; **not yet run against a real instance** |
| OIPReqs standards, policies, procedures, playbooks, templates | Loader over `docs/OIPReqs` (206 documents; the newest version of each) | Working |
| AppDynamics, Splunk | | Not built |

**Ingestion is its own service (box 2).** Saving a document only queues it (`indexed_at IS NULL`). The ingestion service (`sra.knowledge.ingest`) then extracts and normalises, chunks, embeds (FastEmbed/ONNX, `BAAI/bge-small-en-v1.5`, 384-d), stores and links. It runs in the Kafka worker, or in a background thread of a single-process deployment, or as its own process:

```
sra ingest --standards              # load / refresh the OIPReqs library, then ingest what is waiting
sra ingest --servicenow             # sync ServiceNow, then ingest what changed
sra ingest --every 900              # keep running: every 15 minutes
```

An upload request no longer waits for chunking or embedding. An automated pass first ingests anything of its own assessment that is still waiting, so it never runs on half-indexed evidence. One bad document is recorded as failed and does not block the queue.

**Synced records feed assessments.** The rules read the ServiceNow records about an assessment's application (matched by its name and aliases) straight from the store, so an assessment can run without uploading exports of the synced tables. An uploaded export of a table replaces the synced copy of that table, so nothing is counted twice. Records are stored redacted exactly as an uploaded export would be (people fields dropped, secrets and personal data blanked).

**Relationships.** Each result of a search lists the documents it is linked to, in both directions:

| Link | From |
|---|---|
| incident → problem (`problem_id`), incident/problem → change (`rfc`, `caused_by`), record → configuration item (`cmdb_ci`, `business_service`), alert → incident | ServiceNow reference fields (`config/sources.yaml`) |
| configuration item → configuration item, typed ("Depends on::Used by") | `cmdb_rel_ci` rows |
| document → the service it is about (`about`) | Matching a document's application to that application's configuration item |
| standard → the documents it cites (`references`) | Document codes in the text (SRE-PB-08, CMDB-STD-10, ...) |

**Access lists.** Uploads: the `acl` form field. ServiceNow: the fields named per table in `config/sources.yaml` (knowledge articles carry `roles` and `can_read_user_criteria`). ServiceNow's own ACLs are role-based rules that the Table API does not export, so for tables without such fields the connector's service account decides what is copied, and records are readable by every caller unless `acl_fields` is set. The caller is taken from the ingress headers (`X-User` / IAP e-mail, `X-Groups`); the UI has no login of its own, so this is only as strong as the ingress in front of it.

**Retrieval (box 4)** runs in the database:

| Step | Here |
|---|---|
| 1. Keyword search | `websearch_to_tsquery` over the query's words (OR-ed, so a long criterion text still matches), ranked with `ts_rank_cd`, top 50 |
| 2. Semantic vector search | `embedding <=> query` (pgvector), top 50; iterative HNSW scan on pgvector 0.8+ |
| 3. Reciprocal Rank Fusion | In Python (`retrieval.chunks.rrf`, k = 60), the same function the in-memory index uses |
| 4. Optional reranking | `RETRIEVAL_RERANK=auto`: a local cross-encoder (`Xenova/ms-marco-MiniLM-L-6-v2`, ONNX) re-orders the best 20. Off by default |
| 5. Evidence and access validation | Access list filter; every result carries chunk id, document id, source system and source reference; model answers must cite chunks that were retrieved (`llm/verify.py`) |

Standards and playbooks say what should exist, not what does, so they are never retrieved as evidence for a criterion; they are found through the API.

**API (box 5).**

| Call | For |
|---|---|
| `POST /api/search` | The whole store. Filters: `application`, `kinds`, `doc_types`, `source_systems` (`upload`, `servicenow`, `oipreqs`) |
| `POST /api/assessments/{id}/search` | One assessment's files plus the standing records about its application |
| `GET /api/knowledge/documents/{id}` | A document and everything linked to it |
| `GET /api/knowledge/status` | Counts by source, what is waiting or failed, each connector's watermark |
| `POST /api/knowledge/sync` | Refresh the sources and ingest what changed (runs in the ingestion service) |

- **Without pgvector** retrieval is keyword-only and says so; nothing fails. An administrator runs `CREATE EXTENSION vector` once per database.
- **Without a database** (local files) uploads are searched in memory per assessment; the standing store, the connector and the standards need PostgreSQL.
- **Tested** against PostgreSQL 16 + pgvector 0.8 (`tests/test_postgres.py`: set `SRA_TEST_DATABASE_URL`; it creates and drops its own scratch database).
- **To confirm before the first real sync:** the field names and the query scope per table in `config/sources.yaml` (every assumption is marked CONFIRM), and which ServiceNow account the connector may use.

### Fitting into OIP on CAP

SRA is built and run the way OIP (FSIC COG v2) is, following OIP's `CAP_DEPLOYMENT_ARCHITECTURE.md`, `CICD_JFROG_CAP.md` and `GATEWAY_COMPATIBILITY.md` (copies of the deployment diagram: `docs/cog_cap_deployment.svg`). In OIP's readiness assessment it is the "Reliability Assessment Agent — Posture Scoring" against the OIPReqs standards.

| OIP | SRA |
|---|---|
| One long-running container, port 8080: FastAPI + React UI, `/healthz`, scheduler thread in the same process | Same: `sra serve`. Jobs run in-process; `sra.scheduler` runs the knowledge-store ingestion at `SRA_INGEST_HOURS` (and once at start with `SRA_RUN_ON_START`). Kafka + `sra worker` are optional. |
| Single replica (the scheduler is in the web process); a rollout may briefly overlap two pods | Helm pins `api.replicas: 1` unless `kafka.enabled`, and uses the `Recreate` strategy so pods never overlap |
| A database failure at start is logged; the health probe stays up and the UI shows the error | Same: `/healthz` reports `database: error: …`, `/api/*` answers 503 with the reason, and the schema is retried every 30 s |
| `ci.yml` calls `gha_workflow_actions/python_docker.yaml@latest` with the `permissions` block, `ARTIFACT_SCA: "requirements.txt"`, `DOCKER_PUBLISH: true` | Same; tag `sra-<run_number>`. The Python tests run first on a self-hosted runner. |
| Dockerfile: JFrog only; BuildKit secrets `artifactory_user_encoded` / `artifactory_token`; distroless `python-3-debug` builder and `python-3-distroless` runtime; UID 31337; exec-form `CMD` | Same; the npm lockfile is pointed at `cvsdigital-npm` at build time |
| cloud-sql-proxy sidecar on localhost, IAM login, no password; `CAP_POSTGRES_<APP>_<DB>_JDBC_URI` and `CAP_POSTGRES_USER` injected | Same (Helm `cloudSql.useCapInjected: true`, the default). If CAP injects several databases, `SRA_CAP_POSTGRES_VAR` names the one to use. |
| Tables `cog_*`, created on first use; the database is the system of record | Tables and indexes `sra_*` in the same database, created on first use; uploaded evidence files are stored there too (`sra_blobs`). SRA never reads or writes a `cog_*` table. |
| Claude through the LMS gateway; `ANTHROPIC_BASE_URL` set, `ANTHROPIC_API_KEY` from the approved secret store at runtime; forced tool call (the gateway rejects `output_config.format` and `strict`), answers validated, one corrective retry | Same (Helm `llm.provider: anthropic`, `outputMode: tool`, model `claude-opus-5`). `LLM_OUTPUT_MODE=auto` picks tool mode for any address other than api.anthropic.com. |
| No login on the UI: CAP dev environment only | Same limit. Identity comes from ingress headers, which are only trustworthy if the ingress sets them and strips client copies |

To confirm with CAP: the exact injected variable names, whether the SRA service account may create tables in OIP's database, who enables pgvector there, the namespace, ingress and image pull path, and where `ANTHROPIC_API_KEY` is set.

### GitHub repository scan (`sra.connectors.github`)

An assessor can point an assessment at a GitHub repository (Evidence tab → *Scan a GitHub repository*, `POST /api/assessments/{id}/github`, or `sra github owner/name --assessment SRA-… | --out <evidence-dir>`). The server shells out to the GitHub CLI (`gh api`), resolves the ref (default: the default branch) to one commit, and stores what it finds as evidence files exactly like uploads, named `<host>/<owner>/<name>@<sha12>/<path>`. The same parsers, redaction and detectors then score them.

The scan runs as a job, like the automated pass, so a large repository never holds a web request open: the POST checks the input and gh's sign-in at once (422 / 503) and answers 202 with the job; the status panel above the tabs shows the phase, files read, an ETA and the activity log (`GET /api/assessments/{id}/job`), and *Cancel scan* (`POST .../cancel`) stops it between batches. Evidence is stored only at the end, so a cancelled or failed scan adds nothing. One job runs per assessment: a scan and an automated pass wait for each other (409). With Kafka the worker runs the scan, so gh must be installed and signed in there.

| From the repository | Detected as | Used by |
|---|---|---|
| `.github/workflows/*.yml`, `.gitlab-ci.yml`, `Jenkinsfile`, `azure-pipelines.yml`, `cloudbuild.yaml` | `ci_config` | pipeline gates (SRE-AT-11-C04), deployment automation (SRE-AT-12), Jenkins EOL |
| Any YAML declaring `apiVersion` and `kind` | `k8s_manifest` | replicas, probes, limits, PDB/HPA (`config_checks`, `criteria_rules`) |
| Helm charts (`Chart.yaml`, `values*.yaml`, `templates/*.yaml`), Dockerfiles, compose files, `CODEOWNERS`, `.github/dependabot.yml` | `other` (config text) | timeout/retry/breaker checks, LLM-assisted criteria. Helm templates are not rendered, so their workloads are not checked like plain manifests. |
| Runbooks and docs (`docs/`, `runbooks/`, top-level `*.md`, names like *runbook*, *postmortem*, *dr-plan*; MD/RST/TXT/PDF/DOCX) | document type by content (`runbook`, `dr_plan`, …) | checklists, review dates |
| Committed scanner output (`*.sarif`, SBOM/Trivy/Grype JSON) | `scan` | vulnerability findings |
| Open Dependabot alerts (as CycloneDX), code-scanning and secret-scanning alerts (as SARIF; the secret value is dropped) | `scan` | SRE-AT-11-C01/C02/C03, one finding per alert |
| Default branch, branch protection and rulesets, environments, last 10 releases and deployments | document (`github-repository-signals.md`) | LLM-assisted criteria, the assessor |
| Source code (Python, Java/Kotlin/Scala, Go, JavaScript/TypeScript, C#, Ruby, PHP, Rust): analysed in memory, only the signals are stored | `code_analysis` (`github-code-signals.json`) | `code_signals` detector (below) |

**Source code signals** (`sra.evidence.code_signals`, detector `code_signals`). A fast pattern pass over the commit's source (about 1 s per 100 files), tuned for precision: comments and docstrings are ignored, and most rules look only at code outside string literals. The raw code is never stored; each signal keeps the file, the line and a short redacted snippet (none for credentials). Untick *Source code* in the UI, or pass `--no-code` / `"code": false`, to skip it.

| Category | Signals | Criteria (results ask for review) |
|---|---|---|
| Latency / resilience | HTTP calls without a timeout (requests, urlopen, Go default client, HttpURLConnection, Java HttpClient, RestTemplate, axios, server-side fetch); blocking calls inside `async def` | SRE-AT-05-C01 Gap; Met when timeouts are set and none is missing |
| Security | hard-coded credentials; TLS verification off; SQL built from strings; eval/exec; pickle/ObjectInputStream; `yaml.load`; `shell=True`; debug on; wildcard CORS; MD5/SHA-1 | SRE-AT-11-C02 Gap (credentials); a review note on SRE-AT-11-C01 for high-risk patterns |
| Performance | queries or HTTP calls inside `for` loops (N+1); sequential `await` in loops; `SELECT *` | findings |
| Scalability | unbounded thread pools and queues; process-local caches; in-memory session stores; connections opened without a pool | SRE-AT-06-C04 Gap (unbounded); Met when rate limiting is found |
| Coding practice / observability | swallowed exceptions (empty handlers, `except Exception: pass`), bare `except`, print/console logging, no tests | SRE-AT-13-C03 Gap; SRE-AT-03-C01 Gap or Met (structured logger); a review note on SRE-AT-12-C02 when there are no tests |
| Capabilities (positive) | retries with backoff, circuit breakers, rate limiting, structured logging, metrics, tracing, health endpoints, graceful shutdown, caching, pooling | SRE-AT-05-C02, SRE-AT-05-C03, SRE-AT-03-C02 Met; the ones not found become one observation |

Limits: `SRA_GITHUB_MAX_CODE_FILES` (3000), `SRA_GITHUB_MAX_CODE_FILE_KB` (1024), `SRA_GITHUB_MAX_CODE_MB` (60); test, vendored and minified files are not analysed (tests are counted). It is not a SAST tool: it finds common patterns, misses what needs data-flow analysis, and capabilities provided by a framework, sidecar or service mesh do not show up in the code. Thresholds are in `config/detectors.yaml` (`code_signals`).

- **Authentication** is gh's own: `gh auth login` on a workstation, or `GH_TOKEN` (github.com) / `GH_ENTERPRISE_TOKEN` (any other host) in the server's environment from the secret store (Helm `github.existingSecret`). The code never reads, passes or logs a token; gh's error text is redacted before it is shown. A read-only token needs repository contents and, for alerts, Dependabot / code-scanning / secret-scanning read access; alerts it cannot read are reported as "not available" and the rest of the scan still lands.
- **Signing in.** The Evidence tab shows the server's GitHub connection (`GET /api/github/auth`: `gh auth status`, the account, the host and whether the credential is an environment token or gh's own sign-in, never the token). The server is shared and a sign-in stored in its gh config acts for every user (and is lost when a CAP container restarts), so:
  - **Production (CAP):** inject `GH_TOKEN` / `GH_ENTERPRISE_TOKEN` from the secret store. The UI then shows the connection and offers no sign-in or sign-out.
  - **Local and dev:** set `SRA_GH_INTERACTIVE_LOGIN=true` (and no environment token). *Sign in to GitHub* runs gh's device flow (`gh auth login --web`): the UI shows the one-time code and the GitHub URL, and polls until gh reports the sign-in done (`POST`/`DELETE /api/github/login`). The app never asks for a GitHub password. *Sign out* runs `gh auth logout` (`POST /api/github/logout`).
  - **Pasted token** (second opt-in, `SRA_GH_TOKEN_LOGIN=true`): the token goes to `gh auth login --with-token` on stdin and into gh's credential store; the app does not log, return or keep it.
  - **CLI:** `sra github status` (exit code 1 when not signed in), `sra github login` and `sra github logout` (these run gh itself on your terminal).
- **GitHub Enterprise** goes through `GH_HOST`. Only hosts in `SRA_GITHUB_HOSTS` (default: `GH_HOST`, else github.com) can be scanned, so an assessor cannot send the Enterprise token to another host.
- **gh missing or not signed in** answers 503 with what to do; the UI disables the scan and shows the reason (`GET /api/github/status`).
- **Large repositories.** A repository whose size (GitHub's figure, which includes history) is near `SRA_GITHUB_MAX_ARCHIVE_MB` (300), or whose archive turns out bigger than that, is read file by file instead: the commit's file list comes from the git trees API (no content), and only the chosen files are downloaded (git blobs API), `SRA_GITHUB_FETCH_WORKERS` (8) at a time, in batches that stop at the limits. Both ways choose the same files. In this mode at most `SRA_GITHUB_MAX_YAML_PROBES` (300) unnamed YAML files are opened to look for manifests (paths like `k8s/`, `deploy/`, `charts/` first), and `SRA_GITHUB_MAX_CODE_FILES` defaults to 800 (shallow paths first); test files are counted from their paths without being downloaded. Expect about 7 files a second, so a large repository takes a few minutes and uses a few hundred to ~1,500 API requests of the 5,000 per hour a token gets. `SRA_GITHUB_FETCH=auto` (default), `archive` or `files` forces a way.
- **Limits:** `SRA_GITHUB_MAX_FILES` (400), `SRA_GITHUB_MAX_FILE_MB` (5), `SRA_GITHUB_MAX_TOTAL_MB` (50), `SRA_GITHUB_MAX_ARCHIVE_MB` (300), `SRA_GITHUB_TIMEOUT` (120 s per gh call). The tarball is read in memory; nothing is extracted to disk. Vendored and fixture folders (`node_modules`, `vendor`, `testdata`, `fixtures`, …) are skipped.
- **In the image:** gh is one static binary, so it runs in the distroless runtime. The Dockerfile fetches it from JFrog when the build passes `GH_CLI_URL` (the `gh_<version>_linux_amd64.tar.gz` release asset through a JFrog generic remote that proxies `github.com/cli/cli/releases`; CONFIRM the repository name with the platform team) and `GH_CLI_SHA256`. Without `GH_CLI_URL` the image has no gh and the scan says so; nothing else changes. A GitHub Enterprise host signed by an internal CA needs that CA in the image (`SSL_CERT_FILE`).
- **Rescans** add a new snapshot: files whose content is unchanged are not stored twice, but alerts that changed since an earlier scan sit next to the earlier alert files, as with re-uploaded scanner reports (there is no evidence removal yet).

### Images and diagrams in evidence (OCR)

Uploaded images (jpg/png/tiff/webp/bmp), pictures inside DOCX files and images on PDF pages are read with RapidOCR (an ONNX model on CPU whose weights ship inside the wheel, so nothing is downloaded at run time). Each picture's text is placed in the document where the picture appears, between `[Image <name>]` and `[End of image]`, so checklists, rules and retrieval see it like any other text. A scanned PDF is read the same way, page by page.

- OCR gives the **words** on a diagram (component names, labels), not its boxes and arrows, so "what connects to what" cannot be checked from a picture. The bundled model also sometimes joins two English words ("previousrelease").
- Icons and logos are skipped by size; a repeated picture is read once. Each document reads at most `SRA_OCR_MAX_IMAGES` images (20) and `SRA_OCR_MAX_SECONDS` of OCR (60). A dense diagram with ~150 labels takes ~30 s on a laptop CPU. Pictures past a limit are marked "not read" in the text and listed as ingest warnings.
- EMF/WMF/SVG drawings and embedded objects (e.g. a Visio drawing pasted into Word) are noted but not read; for a Visio diagram the note asks for the .vsdx or .drawio file.
- OCR text is cached by image hash in memory and under `SRA_DATA_DIR/ocr_cache`, because evidence is re-parsed whenever it is loaded. `SRA_OCR=false` turns image reading off.

### Template criteria computed from data (`criteria_rules`)

These are 25 criteria that used to go to the LLM, now settled by plain rules over data the app already ingests: ServiceNow incidents, changes, problems, KB articles, alerts and CMDB records, Kubernetes manifests, and CI configuration. Each rule sets a result only when the data it needs is present; otherwise the criterion still goes to the LLM or the assessor. When part of a criterion can't be seen in data (a test record, an approval), the result is flagged for human review. V2 CMDB criteria also get a measured value and a maturity level.

Several rules need fields to be added to existing exports (`time_worked`, `reassignment_count`, `rfc`, `last_discovered`, a KB review owner). Field names and label keys marked CONFIRM under `criteria_rules` in `config/detectors.yaml` must be checked against the instance. The label keys come from CMDB-PB-10 and are pending OD-4.

### ServiceNow tables and registers (`data_rules`)

These are 52 criteria defined on data beyond the core incident, change and CMDB exports:
- the service model (`cmdb_ci_service_auto`, `svc_ci_assoc`, `service_offering`);
- discovery, duplicate and audit tables;
- `task_sla`, `contract_sla` and the service catalog;
- team registers (risk, exceptions, improvements, vendors, contracts, technical debt, action registers, roster, access reviews, automations, toil, deployments) as CSV or XLSX.

Registers are recognised by file name, or by the upload hint `register:<type>`. Their headings are mapped in `config/registers.yaml`. ServiceNow field names that vary between instances go under `data_rules.fields` in `config/detectors.yaml`.

`docs/data-export-request.md` lists what to export, with filters and the criteria each unlocks. All of this was tested on synthetic data only, so confirm the field names with a few real rows.

The scenario kit generates all of these files, and has 14 defects that break them (e.g. `app_service_misnamed`, `sla_breached`, `toil_rising`).

### Document checklists (`checklists`)

These are 37 criteria that ask whether the evidence contains specific things: RTO/RPO values and a recent DR test, a toil inventory with weekly hours, a RACI with no blank cells, a KPI catalogue naming the five KPIs, and so on. Each has a checklist in `config/checklists.yaml`. The checklist says which documents to look in (by file name or title, or by document type), which items must be present (keyword patterns, minimum counts, things that must not appear), and optionally freshness or a monthly cadence. Two criteria apply their checklist to each record (every P1 incident's notes; each KB article). Three are small dedicated checks:
- RTO/RPO in the documents against the CMDB and the tier limits;
- a continuity review dated after the latest P1/P2;
- a KB search for the top incident categories.

How a result is decided:
- **No matching document:** nothing is set, and the criterion goes to the assessor.
- **Every item found:** Met. A few checklists still ask for review because they need an approval or count the documents can't show.
- **Anything missing:** Gap, naming what's missing, and always flagged for review. Wording the patterns don't expect can hide an item that's really there.

**Line suggestions (non-generative model, advisory).** Items are decided by keywords only. When keywords miss an item, a small natural-language-inference model (`cross-encoder/nli-deberta-v3-xsmall`, about 96 MB as ONNX, run with `onnxruntime`) checks the candidate documents for a line that entails the item's plain-language requirement (`config/checklist_requirements.yaml`, 108 items). If one scores 0.9 or more, the missing item says "possibly covered by '…' (model suggestion, please confirm)".
- **It never decides anything.** A suggestion never makes an item present, so the criterion stays a Gap, flagged for the reviewer.
- **What it found when measured** (`eval/check_matchers.py`, test set `eval/checklist_matching.yaml`): 11 of 24 reworded items that keywords missed (keywords found 2). It also matched some unrelated lines, which is why it may only suggest. Embeddings and a search reranker did no better than keywords.
- **Turning it on.** The model is read from the local Hugging Face cache only and is never downloaded by the app; the command to fetch it is in `src/sra/retrieval/nli.py`. In the cluster (`HF_HUB_OFFLINE=1`) the files must be mirrored into the image or a volume (`HF_HOME`). Without it, suggestions are off and the status line says why. `SRA_SUGGEST=off` turns them off explicitly.
- **Cost:** about 16 ms per line on a laptop CPU, and it only runs for missing items.

**AI second opinion (runs with AI evaluation on).** When a checklist Gap comes only from keywords not matching (not from a count, a "TBD"-style phrase, a stale date or a missing monthly record), the local LLM is asked about that criterion too, since the document may use other wording. A verified Met or Gap from the model replaces the keyword Gap, keeps the checklist's notes and evidence, and is always flagged for review. If the model finds no evidence, fails, or answers Insufficient or N/A, the checklist's Gap stands with a note. Rules-only runs are unchanged.

The checklists and requirements are a DRAFT written from the template text, so SREs should review them.

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

Editing and deleting: the assessment page has **Edit** and **Delete**.
- **Edit** changes the application profile (name, CMDB ID, tier, characteristics…), the assessment date and the assessor, and the change is recorded in the audit trail. Existing results are kept. If the profile or date changed after an automated pass, the page says to re-run it.
- **Delete** asks you to type the application name. It then permanently removes the assessment, its evidence files, results, findings, job and audit trail. A record of the deletion (id, application, who, when) is kept, and a `deleted` event is sent so other systems can remove their copies. SRE decisions on criteria are kept as training data, each with up to four evidence passages it was based on (`data/feedback/<id>.jsonl`, or the `sra_feedback` table in Postgres). `SRA_KEEP_FEEDBACK=false` deletes them too.

Training labels: every SRE decision on a criterion is recorded as feedback, with its kind:
- **confirm:** agreed with the rule or LLM result;
- **override:** changed it;
- **fill:** decided a criterion nothing had evaluated;
- **revise:** changed an earlier human decision.

Agreements are labels too, so a future classifier sees what automation gets right as well as what it gets wrong.

LLM off: `LLM_ENABLED=false` turns off every generative-model feature, for when only non-generative models are allowed.
- **Runs** are rules and checklists only, even if a client or queued job asks for the LLM.
- **Criterion chat** is off, and the UI hides both options.
- **The Criteria tab** shows how many criteria have no result and need the assessor's decision, with a filter for them.

The rating comes from the deterministic rubric rules, so it doesn't depend on the LLM.
- **When they're refused:** both are refused while an automated pass is running, because the run would overwrite an edit when it saves. Edit is also refused once the assessment is signed off.
- **API:** `PATCH /api/assessments/{id}` and `DELETE /api/assessments/{id}`.

Runs: the assessment page shows live progress for an automated pass and has a **Cancel run** button. Cancelling stops the run at its next step, or within a second during a model call (the request is streamed, and closing it makes Ollama stop generating). Nothing from a cancelled run is saved, so the assessment keeps its previous results. The API equivalent is `POST /api/assessments/{id}/cancel`; it works for the in-process runner and the Kafka worker alike.

LLM speed: on CPU, most of each call's time goes on reading the prompt. So the app sends less, and only when it must:
- **Fewer calls.** Rules and checklists settle what they can first. A re-run reuses the previous answer for a criterion when the model, prompt version and evidence passages are unchanged (`LLM_CACHE`). Re-evaluating a single criterion always asks again.
- **Shorter prompts.** Each criterion gets `LLM_TOP_K` passages (default 4), each cut to the `LLM_CHUNK_CHARS` (default 800) that best match it. Passages are dropped rather than overflowing `LLM_NUM_CTX`; Ollama would otherwise cut the prompt from the front.
- **Shorter replies.** The schema caps the length of each field, and `LLM_NUM_PREDICT` caps the reply.
- **No reloading.** `LLM_KEEP_ALIVE` (default 30m) keeps the model loaded between runs. Set `LLM_NUM_THREAD` to your physical core count.
- **Indexing once.** The search index and embedding model are built once per evidence set or process, and criterion chat reuses them.
- **Parallel calls.** `LLM_CONCURRENCY` sets the calls in flight per server. Left empty, it follows `OLLAMA_NUM_PARALLEL` when the app can see that variable (e.g. a Windows user variable shared with a local Ollama). Ollama never reads the app's `.env`, so set `OLLAMA_NUM_PARALLEL` where Ollama starts. `LLM_BASE_URL` can list several servers, comma-separated, and each criterion goes to whichever server is free next. A server that fails its health check is skipped with a warning, and a cancel stops every call in flight. On one CPU machine the gain is small, because parallel calls share the same cores and prompt reading already uses all of them. Several Ollama replicas, or a GPU server, is where this pays off. To measure it, compare a run at `LLM_CONCURRENCY=1` with one at `2` (with `OLLAMA_NUM_PARALLEL=2` set on the server).

The activity log shows, per criterion, the prompt tokens Ollama had to read, the reply tokens and the seconds each took, plus averages at the end. That tells you which setting matters for your machine. To compare models, run `python eval/run_eval.py --all --llm --model <tag>` and keep the smallest one that holds accuracy.

UI: `python -m sra.cli serve --build` builds the web UI (running `npm install` the first time) and then starts the API; open http://localhost:8080. Without `--build` it serves the existing `web/dist`.

Local LLM: install Ollama and run `ollama pull qwen3.5:4b` (the laptop default; set `LLM_MODEL` to change it). Thinking mode is off by default (`LLM_THINK=false`), because reasoning tokens are too slow on CPU. For the cluster, the candidate is `gemma4:26b`, a mixture-of-experts model that uses about 4B parameters per token; confirm it with the eval. Then add `--llm` to `assess`, or tick "Use local LLM" in the UI. To compare models, use `python eval/run_eval.py --llm --model <a> --model <b>`.

Full stack: `docker compose up -d postgres ollama api` (Postgres+pgvector, Ollama, and the single app container, as on CAP). The optional Kafka topology: `docker compose --profile kafka up -d`.

Postgres tests: `docker compose up -d postgres`, then run pytest with `SRA_TEST_DATABASE_URL=postgresql://sra:sra-dev-only@localhost:5432/sra`.

## Deploying to GKE (CAP)

`deploy/helm/sra`, by default the OIP shape:
- one app container (1 replica, `Recreate`, probes on `/healthz`, non-root, read-only root filesystem) with a native-sidecar cloud-sql-proxy using IAM auth, on the CAP-injected database;
- Claude through the LMS gateway (`llm.gateway.existingSecret` must name the secret holding `ANTHROPIC_API_KEY`);
- ingestion on the in-process scheduler (`ingestion.hours`, `ingestion.runOnStart`).

Optional: `kafka.enabled` (Kafka worker Deployment, several API replicas), `gcs.bucket` (evidence files in GCS), `ollama.enabled` with `llm.provider: ollama` (an Ollama StatefulSet on a CPU high-memory node pool; weights mirrored to GCS and copied into the PVC by an init container), `ingestion.schedule` (a CronJob instead of the scheduler).

The image builds through `.github/workflows/ci.yml`, using the shared `python_docker.yaml` workflow. All base images come from JFrog.

## Open items (need stakeholder confirmation)

- The rubric, the scoring weights and the maturity bands are V1 drafts.
- The crosswalk is curated by hand.
- `[CONFIRM]` ServiceNow tables and fields in `config/detectors.yaml` (e.g. `u_slo_definition`, `u_business_application`).
- pgvector on the Cloud SQL instance; UI ingress/IAP or SSO (OIP has the same open item); pinning the shared workflow instead of `@latest`. Only with the optional extras: mirroring model weights, Kafka topics and auth.
- Duplicate playbooks: SRE-PB-11/14 and SRE-PB-13/15 (the crosswalk uses the lower number).
