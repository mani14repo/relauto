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
| Evidence | `sra.evidence` | ServiceNow CSV, XLSX and Table API JSON (incident, change_request, problem, kb_knowledge, em_alert, cmdb_ci, cmdb_rel_ci); docx/pdf/md/txt; images (jpg/png/tiff/webp) and scanned PDFs via RapidOCR (CPU, weights inside the wheel); Trivy/Grype/CycloneDX/SARIF; Kubernetes YAML; Jenkinsfile/GitHub Actions |
| Detectors | `sra.detectors` | `rubric_rules` (all 18 Evidence Catalog rules), `criteria_rules` (25 template criteria computed from ingested data), `checklists` (37 document checklists, `config/checklists.yaml`), `data_rules` (52 criteria from extra ServiceNow tables and registers), `incident_anomalies`, `incident_clusters`, `vulnerabilities`, `config_checks`. Thresholds are in `config/detectors.yaml` |
| LLM | `sra.llm`, `sra.retrieval` | BM25, plus optional local ONNX embeddings fused with RRF. Per-criterion JSON-schema calls; `verify.py` rejects invented citations |
| Scoring | `sra.scoring` + `config/scoring_matrix.yaml` | See below |
| Reports | `sra.report` | `docx_fill.py` edits the original template XML in place; `summary.py` produces the rubric Section 8 output |
| Service | `sra.service`, `sra.api`, `sra.worker`, `sra.store` | FileStore for dev; PgStore with a transactional outbox → Kafka |

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
- **Delete** asks you to type the application name. It then permanently removes the assessment, its evidence files, results, findings, job and audit trail. A record of the deletion (id, application, who, when) is kept, and a `deleted` event is sent so other systems can remove their copies. SRE decisions on criteria are kept as training data, each with up to four evidence passages it was based on (`data/feedback/<id>.jsonl`, or the `feedback` table in Postgres). `SRA_KEEP_FEEDBACK=false` deletes them too.

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
