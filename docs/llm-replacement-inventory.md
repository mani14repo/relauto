# Replacing the LLM: criterion inventory

Every criterion the rules did not settle before group 1 was built, sorted by what could replace the local LLM for it. Of the 215 criteria, 53 were settled by the rules in at least one scenario; the other 162 are listed here. Found by running all 41 templates over the 10 eval scenarios plus two scenario-kit cases (all defects, healthy), then sorting each criterion by reading its acceptance criteria, formula and required evidence. The sort is a judgement from the template text; an SRE should check it, especially groups 3–5.

**Status:** group 1 is built (25 criteria: `sra.detectors.criteria_rules`, and CMDB-AT-11-C04 in `rubric_rules`). Each rule sets a result only when its data is in the evidence; otherwise the criterion still goes to the LLM or the assessor. Group 3 is built too (37 of 38: `sra.detectors.checklists`, checklists in `config/checklists.yaml`; CMDB-AT-06-C01 is not, as it needs an approved chain definition whose format is not known). Group 2 is built for 52 of 82 criteria (`sra.detectors.data_rules`): the standard ServiceNow service-model, discovery, audit, SLA and catalog tables, and team registers read as CSV or XLSX (`config/registers.yaml`). The other 30 need tool-specific inventories (APM, cloud billing, telemetry mapping, DBA/middleware inventories) or approval records whose format is not known. Nine criteria first placed in group 1 were moved once their rules were looked at in detail (noted below).

| Group | Criteria | What replaces the LLM | Needs |
|---|---|---|---|
| 1. Rule on data already ingested | 25 (built) | A deterministic rule (formula, count, date check) | 8 work fully only once an extra field is in the existing export |
| 2. Rule on a new export | 82 (52 built) | A deterministic rule, once the data is exported | New ServiceNow tables, registers or tool exports, plus a parser for each |
| 3. Document checklist | 38 (37 built) | Checklist of required elements, matched in uploaded documents; extraction of dates and values (RTO/RPO, owners) | The documents; keyword patterns; matching by meaning was tried and turned off (see config/checklists.yaml) |
| 4. Trained classifier | 6 | A classifier on (criterion, evidence) → Met/Gap, trained on SRE decisions; human review until then | A few hundred labelled decisions |
| 5. Human only | 11 | Assessor decides; the app prepares the evidence | — |

**Groups 1–2 (107, 66%) need no AI at all.** The templates already call most of them deterministic and give the formula; the LLM only got them because no rule had been written, or because the data isn't exported. Group 3 (38) is where most of the LLM's real reading work is. Groups 4–5 (17) are judgement calls.

Two caveats:
- **Group 2 depends on data access.** The rules run only when the exports or registers are uploaded; field names and register column headings are guesses (CONFIRM in `config/detectors.yaml` under `data_rules.fields`, and `config/registers.yaml`) and were tested on synthetic data only. Without the data those criteria fall to the assessor, which is also what happens when the LLM finds no evidence.
- **Group 3 uses keyword patterns.** Documents worded in ways the patterns do not expect come out as Gaps, so every checklist Gap is flagged for review. Matching by meaning with bge-small was measured and turned off: it scored unrelated lines as high as real paraphrases.

**Moved out of group 1:** CMDB-AT-06-C01 (needs the approved chain definition document) → 3; CMDB-AT-09-C02, CMDB-AT-10-C06, ITSM-AT-03-C04, SRE-AT-10-C02 (need data not in today's exports) → 2; CMDB-AT-08-C04 (the assessor confirms which incidents a change caused) and CMDB-AT-13-C02/C03/C04 (the app produces these itself; they are programme-level checks) → 5.

## 1. Rule on data the app already ingests (25)

| Criterion | Name | Template says | How | Built |
|---|---|---|---|---|
| CMDB-AT-01-C03 | Portfolio placement attributes | deterministic | cmdb_ci business app: u_region, u_portfolio, busines_criticality and life cycle valid (CMDB-STD-02 R06–R07) — **needs field:** u_portfolio, life_cycle_stage (names pending OD-1) | yes |
| CMDB-AT-02-C04 | Stale CI control | deterministic | cmdb_ci.last_discovered > 30 days — **needs field:** last_discovered | yes |
| CMDB-AT-07-C03 | Required labels/tags present | deterministic | required labels in the Kubernetes manifests already ingested | yes |
| CMDB-AT-08-C01 | Affected CI populated | deterministic | change_request.cmdb_ci populated | yes |
| CMDB-AT-10-C04 | Tier drives priority and engagement | deterministic | incident priority vs service tier | yes |
| CMDB-AT-11-C04 | Completeness meets tier threshold | deterministic | same completeness calculation as P2-10 / CMDB-AT-04-C02 | yes |
| CMDB-AT-12-C04 | Workload metadata standard adopted | deterministic | required metadata keys in the Kubernetes manifests | yes |
| CMDB-AT-12-C05 | Event and alert CI binding | deterministic | em_alert.cmdb_ci populated | yes |
| ITSM-AT-01-C05 | Jenkins Migration Compliance (Oct 2026 Deadline) | deterministic | Jenkins found in CI config and deployments | yes |
| ITSM-AT-02-C03 | Work-Time Capture | deterministic | incident time_worked populated — **needs field:** time_worked | yes |
| ITSM-AT-03-C03 | Problem-to-Change Linkage | deterministic | problem with fix recommended has a linked change (rfc field) — **needs field:** rfc | yes |
| ITSM-AT-04-C01 | First-Call Resolution (FCR) Rate | deterministic | reassignment_count = 0 share for PSE incidents — **needs field:** reassignment_count | yes |
| ITSM-AT-04-C03 | Queue Management & Backlog Age | deterministic | open incident ages by priority — **needs field:** open records | yes |
| ITSM-AT-07-C01 | Knowledge Article Coverage for Top Incident Categories | deterministic | KB articles per top-20 incident category (category and text match) | yes |
| ITSM-AT-07-C02 | Article Currency & Review Cadence | deterministic | kb_knowledge review date within 6 months and owner set — **needs field:** a review-owner field | yes |
| ITSM-AT-07-C04 | Knowledge Contribution Rate | deterministic | kb_knowledge new/updated per team per month | yes |
| ITSM-AT-09-C02 | Policy Compliance for Critical Controls | deterministic | change compliance (P1-07 data) and Jenkins status | yes |
| ITSM-AT-09-C03 | Audit Readiness | human | field completeness on sampled change, problem and incident records | yes |
| ITSM-AT-11-C02 | Operational reporting data quality meets accuracy threshold | deterministic | required incident fields populated >= 95% | yes |
| SRE-AT-06-C05 | Scaling Behaviour Tested | deterministic | HPA CPU target in the Kubernetes manifests; scale test record is a document | yes |
| SRE-AT-09-C01 | Post-Mortem Coverage for Significant Incidents | deterministic | P1/P2 incidents vs problem / post-mortem dates — **needs field:** problem opened_at (for the 5-day check) | yes |
| SRE-AT-12-C01 | Pipeline Existence & Automation Level | deterministic | CI config and deployment records: automated, no Jenkins | yes |
| SRE-AT-12-C02 | Quality Gate Coverage | deterministic | pipeline gate check already exists (findings only today) | yes |
| SRE-AT-12-C05 | Rollback Automation | human | rollback stage in pipeline config; test date is a document | yes |
| SRE-AT-13-C03 | Integration Error Handling | human | retries/timeouts already checked in config (P2-04); code review stays human | yes |

## 2. Rule on a new ServiceNow or tool export (82)

| Criterion | Name | Template says | How | Built |
|---|---|---|---|---|
| CMDB-AT-01-C04 | Application Service per environment, correctly named and linked | deterministic | cmdb_ci_service_auto per environment; naming regex; rel to BA | yes |
| CMDB-AT-01-C05 | Service Offering linkage (business and technical) | deterministic+ai | service_offering links for Tier 1/2 Application Services | yes |
| CMDB-AT-01-C07 | Production telemetry identity registered | deterministic | telemetry source map vs CMDB-AT-12 inventory | no |
| CMDB-AT-02-C01 | Discovery scope coverage | deterministic | discovery_status: sources with a run in 7 days | yes |
| CMDB-AT-02-C02 | IRE identification and data-source precedence | deterministic+human | cmdb_identifier coverage; sys_audit overwrites (sample confirmed by a person) | yes |
| CMDB-AT-02-C03 | Duplicate CI control | deterministic | reconcile_duplicate_task: rate and age per class | yes |
| CMDB-AT-02-C05 | Post-discovery enrichment | deterministic | support_group, used_for, svc_ci_assoc on discovered CIs | yes |
| CMDB-AT-02-C06 | Discovery health and credential hygiene | deterministic | discovery_device_history error rate; credential rotation dates | yes |
| CMDB-AT-03-C02 | Life cycle transition governance | deterministic | sys_audit state transitions with a change_request | yes |
| CMDB-AT-03-C03 | Hosting CI linked to Application Service | deterministic | svc_ci_assoc: hosting CIs linked to an Application Service | yes |
| CMDB-AT-03-C04 | Asset-to-CI linkage (hardware) | deterministic | alm_hardware to cmdb_ci_hardware join | yes |
| CMDB-AT-03-C05 | Decommission completeness | deterministic+human | decommission changes vs six conditions (rels, alerts, config maps) | no |
| CMDB-AT-03-C06 | Software normalization for EOL (conditional) | deterministic | software installs normalized to product models | yes |
| CMDB-AT-04-C01 | CMDB Health configured and baselined for FSI scope | deterministic+human | CMDB Health config per class plus a dated baseline record | no |
| CMDB-AT-04-C03 | Correctness: duplicates, orphans, staleness | deterministic | CMDB Health correctness scores vs TH-10/11/12 | no |
| CMDB-AT-04-C05 | Remediation workflow with SLA | deterministic | remediation tasks: closed within SLA, owner set | yes |
| CMDB-AT-05-C01 | Server and VM accuracy | deterministic | server/VM attributes vs discovery or cloud API inventory | no |
| CMDB-AT-05-C02 | Database accuracy | deterministic | database CIs vs DBA inventory export | no |
| CMDB-AT-05-C03 | Middleware and app-server accuracy | deterministic | middleware CIs vs inventory export | no |
| CMDB-AT-05-C04 | Network and load-balancer accuracy | deterministic | Tier 1 VIPs registered and linked | no |
| CMDB-AT-05-C05 | Container platform accuracy | deterministic | clusters/namespaces vs cloud API inventory | no |
| CMDB-AT-05-C06 | Configuration drift detection | deterministic | sys_audit changes without a change_request, triage time | no |
| CMDB-AT-06-C02 | Service population (no empty services) | deterministic | svc_ci_assoc: services with at least one CI | yes |
| CMDB-AT-06-C03 | Dependencies classified | deterministic+ai | dependency classification attributes (companion table, CONFIRM) | no |
| CMDB-AT-06-C04 | Shared platforms modeled as Technical Service Offerings | deterministic | technical service offerings for shared platforms | no |
| CMDB-AT-06-C05 | Validated against observed traffic | deterministic+human | observed calls (AppDynamics/Splunk export) vs modeled relationships | no |
| CMDB-AT-07-C01 | Cloud account coverage | deterministic | cloud accounts discovered vs account inventory | no |
| CMDB-AT-07-C02 | Kubernetes cluster, namespace and workload registration | deterministic | namespaces mapped to one Application Service | yes |
| CMDB-AT-07-C04 | Ephemeral resource retirement | deterministic | cloud CIs unseen > 7 days still Installed | yes |
| CMDB-AT-07-C06 | Cloud cost attribution (conditional) | deterministic | cloud billing export: attributed spend share | no |
| CMDB-AT-08-C02 | Impacted Services calculated from CMDB | deterministic | task_cmdb_ci_service populated before approval | yes |
| CMDB-AT-08-C05 | Post-change CMDB update | deterministic | sys_audit CMDB updates within TH-14 of change close | no |
| CMDB-AT-08-C06 | Conflict detection uses CMDB | deterministic | change conflict status populated | yes |
| CMDB-AT-09-C01 | EOL dates recorded on product models | deterministic | product models with an EOL date | yes |
| CMDB-AT-09-C02 | Exposure computed through CMDB | deterministic | EOL items need product models / software installs linked to CIs | no |
| CMDB-AT-09-C04 | Risk acceptance for deferrals | deterministic | risk register: deferrals with valid unexpired acceptance | yes |
| CMDB-AT-09-C05 | Technical debt linked to Business Applications | deterministic | debt register items linked to a Business Application | yes |
| CMDB-AT-09-C06 | Burn-down velocity | deterministic | register: Critical/High closed per quarter | yes |
| CMDB-AT-10-C01 | Tier designated and approved | deterministic | tier field plus approval record | yes |
| CMDB-AT-10-C05 | Tier drives data-quality obligations | deterministic | health rules configured per tier | no |
| CMDB-AT-10-C06 | Dependency tier inheritance | deterministic | needs which dependencies are Critical (CMDB-AT-06-C03 classification, CONFIRM) | no |
| CMDB-AT-11-C01 | Attestation current by tier | deterministic | attestation dates by tier | yes |
| CMDB-AT-11-C02 | Data certification campaigns configured | deterministic | cert_policy / cert_filter coverage and completion | no |
| CMDB-AT-11-C03 | Audit findings within SLA | deterministic | finding tasks within SLA by severity | yes |
| CMDB-AT-11-C05 | Exceptions controlled | deterministic+ai | exception records valid and unexpired | yes |
| CMDB-AT-12-C01 | Telemetry source inventory complete | deterministic | platform sweep vs source inventory | no |
| CMDB-AT-12-C02 | Canonical identity tuple mapped | deterministic | mapping store: identity tuple per source | no |
| CMDB-AT-12-C03 | Environment discrimination unambiguous | deterministic | environment in mapping vs naming; ML can flag odd names | no |
| CMDB-AT-12-C06 | Mapping governance and drift detection | deterministic | unmapped identifiers older than 5 business days | no |
| CMDB-AT-12-C07 | Telemetry retrievable for reliability metrics | deterministic | 28-day series retrievable per Tier 1/2 service | no |
| CMDB-AT-13-C01 | Single approved scope list | deterministic | scope list approval; incident count variance vs Incident Management | no |
| ITSM-AT-03-C04 | RCA-to-Fix Cycle Time | deterministic | RCA completion date needs problem state history (sys_audit) | yes |
| ITSM-AT-04-C02 | Escalation Quality & Timeliness | deterministic | escalation times need assignment history (sys_audit or metric_instance) | no |
| ITSM-AT-04-C05 | Staffing Coverage & Key-Person Risk | human | roster export: at least 2 agents per queue and shift | yes |
| ITSM-AT-05-C01 | Service Coverage | deterministic | catalog items vs CMDB application list | yes |
| ITSM-AT-05-C02 | Ownership & SLA Assignment | deterministic | catalog item fulfillment_team, owner, SLA populated | yes |
| ITSM-AT-05-C05 | Catalog-to-CMDB Alignment | deterministic | catalog item linked to a CI or Business Service | yes |
| ITSM-AT-06-C01 | SLA Definition & Applicability | deterministic | contract_sla definitions for P1-P4 | yes |
| ITSM-AT-06-C02 | SLA Measurement & Reporting | deterministic | task_sla attainment by month | yes |
| ITSM-AT-06-C04 | SLA Breach Trending & Root Cause | human | breach register: causes categorized, top 3 with actions (ML can group causes) | yes |
| ITSM-AT-06-C05 | Corrective Action SLA Compliance | deterministic | corrective action register: done by due date | yes |
| ITSM-AT-07-C03 | OneNote / Email Knowledge Migration | deterministic | migration tracker; OneNote pointers are already detected | yes |
| ITSM-AT-08-C01 | Improvement Opportunity Register | deterministic | improvement register fields populated | yes |
| ITSM-AT-08-C02 | Value-Based Prioritization | human | register: top-5 by score scheduled this quarter | yes |
| ITSM-AT-08-C03 | Improvement Delivery Velocity | deterministic | register: delivered on schedule per quarter | yes |
| ITSM-AT-08-C04 | Improvement Impact Measurement | human | register: before/after metric pair present | yes |
| ITSM-AT-09-C01 | IT Risk Register Existence & Coverage | deterministic | risk register fields populated | yes |
| ITSM-AT-09-C05 | Policy Exception Management | deterministic | exception register: approval, expiry, compensating control | yes |
| ITSM-AT-10-C01 | Vendor Inventory & Dependency Mapping | deterministic | vendor inventory linked to applications in CMDB | yes |
| ITSM-AT-10-C02 | Contract & SLA Coverage | deterministic | contract register: SLA, penalty, review date | yes |
| ITSM-AT-10-C05 | Single-Vendor Concentration Risk | human | vendor-to-application counts > 5; mitigation plan exists | yes |
| SRE-AT-02-C05 | Risk Mitigation for Critical Paths | human | risk register: High/Critical with mitigation, owner, date | yes |
| SRE-AT-03-C04 | Distributed Tracing Coverage | ai | APM export: traces for top 3 transactions, correlation IDs | no |
| SRE-AT-09-C03 | Post-Mortem Action Item Completion | deterministic | post-mortem action items (problem_task) done on time | yes |
| SRE-AT-10-C02 | Toil-as-Percentage-of-Engineering-Capacity | deterministic | toil share needs engineering capacity as well as time worked | yes |
| SRE-AT-10-C04 | Automation Delivery & Realized Value | deterministic | automation delivery records with before/after | yes |
| SRE-AT-10-C05 | Toil Reduction Trend | deterministic | quarterly toil percentage trend | yes |
| SRE-AT-11-C05 | Access Control & Privilege Review | deterministic | access review export: quarterly, no shared accounts | yes |
| SRE-AT-12-C04 | Deployment Frequency & Cycle Time | deterministic | deployment log: frequency and lead time | yes |
| SRE-AT-13-C02 | Integration Health Monitoring | ai | monitor export per integration with thresholds | no |
| SRE-AT-13-C05 | Integration Change Impact Coverage | deterministic | integration changes list consumers in impacted CIs (task_ci) | no |
| SRE-AT-14-C04 | Automation operational ownership is assigned and active | deterministic | automation registry owner and recent activity | yes |

## 3. Document checklist (38)

| Criterion | Name | Template says | How | Built |
|---|---|---|---|---|
| CMDB-AT-04-C04 | Governance roles staffed | human | governance charter: roles named, apps per steward, RACI approval | yes |
| CMDB-AT-04-C06 | Reporting cadence and trend | deterministic | one scorecard per month in governance minutes (dates) | yes |
| CMDB-AT-06-C01 | Critical chain coverage | deterministic | compare cmdb_rel_ci hops with the approved chain definition document | no |
| CMDB-AT-09-C03 | Remediation plan quality | human | remediation plan has owner, target, date, steps | yes |
| CMDB-AT-10-C02 | Business impact analysis current | ai | BIA document exists and is dated within the period | yes |
| CMDB-AT-10-C03 | RTO/RPO recorded and consistent | deterministic+ai | extract RTO/RPO from the continuity plan; compare with CMDB values | yes |
| CMDB-AT-13-C05 | Scorecard published and acted on | deterministic | one governance pack per month (dates) | yes |
| ITSM-AT-02-C05 | Major Incident (P1) Process Adherence | human | P1 work notes: bridge opened, stakeholders notified, timeline within 1 h | yes |
| ITSM-AT-04-C04 | Knowledge Base Accessibility | human | search the KB for each top-10 incident category (semantic match) | yes |
| ITSM-AT-06-C03 | OLA Formalization for Inter-Team Handoffs | human | OLA per handoff with a response target | yes |
| ITSM-AT-07-C05 | AI Readiness of Knowledge Content | human | article structure: trigger, steps, expected outcome, validation | yes |
| ITSM-AT-08-C05 | Governance Cadence Adherence | deterministic | meeting records for each of the last 3 months | yes |
| ITSM-AT-10-C03 | Vendor Incident Escalation Path | human | named contact per critical vendor; test date within 6 months | yes |
| ITSM-AT-10-C04 | Vendor Performance Measurement | human | a vendor scorecard each quarter | yes |
| ITSM-AT-11-C01 | KPI definitions and calculation methods are documented and approved | ai | KPI catalogue: the five KPIs with definition and owner | yes |
| ITSM-AT-11-C03 | MTTR and human-versus-automated demand segmentation are being reported | ai | MTTR report with mean, P90 and segmentation (the app can compute them to compare) | yes |
| ITSM-AT-11-C04 | Reporting ownership and data certification are established | deterministic+ai | report owner and certification record with name and date | yes |
| ITSM-AT-12-C01 | Recovery objectives (RTO and RPO) are defined, approved, and achievabl | deterministic+ai | extract RTO/RPO values, sign-off and DR test date | yes |
| ITSM-AT-12-C03 | Business continuity requirements are integrated with DR procedures | ai | DR procedure references BIA values; owner approval | yes |
| ITSM-AT-12-C04 | Continuity plan is reviewed after major incidents or architecture chan | deterministic+ai | continuity review dated after each P1/P2 (incident dates are ingested) | yes |
| SRE-AT-02-C04 | Recovery Design — RTO/RPO | deterministic | RTO/RPO values and a DR test within 12 months (DR test detection exists) | yes |
| SRE-AT-04-C04 | SLO Attainment Reporting | human | monthly SLO attainment reports for 3 months | yes |
| SRE-AT-05-C05 | Graceful Degradation | human | degraded mode for top 3 failure scenarios in the design doc | yes |
| SRE-AT-07-C05 | Runbook Automation Opportunity Identification | human | at least 3 automation candidates listed or ticketed | yes |
| SRE-AT-08-C04 | Knowledge Transfer & Cross-Training | human | cross-training log: at least 2 engineers | yes |
| SRE-AT-08-C05 | PSE/SRE/Engineering RACI Clarity | human | RACI table has no blank cells and is confirmed | yes |
| SRE-AT-10-C01 | Toil Inventory by Category | human | toil inventory: top 5 with frequency and time | yes |
| SRE-AT-10-C03 | Automation Opportunity Scoring | human | scored automation register; top 2 in backlog | yes |
| SRE-AT-13-C04 | Integration SLA Definition & Measurement | deterministic | integration SLA document with targets per integration | yes |
| SRE-AT-14-C01 | Automation safety controls are implemented: testing, observability, an | ai | test evidence, failure alerting, rollback procedure | yes |
| SRE-AT-14-C02 | Automation authorization and human oversight process is in place | ai | change approval, documented boundaries, escalation path | yes |
| SRE-AT-14-C03 | Automation failure handling and recovery procedures are defined | ai | runbook sections plus review within 12 months | yes |
| SRE-AT-15-C03 | Regression detection criteria are defined and applied | deterministic+ai | regression thresholds documented; check recorded on latest change | yes |
| SRE-AT-15-C04 | Rollback decision process and authority are documented | ai | objective rollback criteria, named authority, procedure | yes |
| SRE-AT-16-C01 | Toil is identified, classified, and baselined for this service | ai | toil inventory: 3+ items with hours, flag, baseline date | yes |
| SRE-AT-16-C02 | Toil reduction targets are set and tracked against the baseline | deterministic+ai | target >= 20%, tracked quarterly | yes |
| SRE-AT-16-C03 | Top toil items have identified automation candidates with prioritizati | ai | top 3 evaluated; one with ROI and owner | yes |
| SRE-AT-16-C04 | Engineering capacity released by automation is measured and reinvested | ai | before/after capacity and reinvestment record | yes |

## 4. Classifier trained on SRE decisions (6)

| Criterion | Name | Template says | How |
|---|---|---|---|
| CMDB-AT-08-C03 | Infrastructure impact assessment quality | human+ai | quality of infrastructure impact assessments (sampled changes) |
| ITSM-AT-01-C02 | Risk Assessment Quality | human | fields populated is a rule; 'risk level justified' needs a classifier |
| ITSM-AT-03-C02 | Root-Cause Analysis Quality | human | root-cause specificity |
| ITSM-AT-05-C04 | User-Facing Description Quality | human | readability and jargon scores help; 'understandable' is a judgement |
| SRE-AT-09-C02 | Post-Mortem Quality — Root Cause Specificity | human | post-mortem root-cause specificity |
| SRE-AT-09-C04 | Blameless Post-Mortem Culture | human | blameless tone |

## 5. Human only (11)

| Criterion | Name | Template says | How |
|---|---|---|---|
| CMDB-AT-03-C01 | Life cycle state accuracy | deterministic | state vs observed reality needs a sample; Retired-on-open-records count is computable |
| CMDB-AT-07-C05 | Hybrid consistency | human | whether a hybrid app has one unified model is a judgement |
| CMDB-AT-08-C04 | Change-caused incident linkage | deterministic | the app measures correlated incidents and the caused_by rate; the assessor confirms which were change-caused |
| CMDB-AT-11-C06 | Independent accuracy sample | human | independent accuracy sample |
| CMDB-AT-13-C02 | CMDB Readiness Index computed | deterministic | produced by this app; monthly coverage across in-scope BAs is a programme check |
| CMDB-AT-13-C03 | Maturity level assigned | deterministic | produced by this app; programme targets across BAs are for the assessor |
| CMDB-AT-13-C04 | Reporting gate decisions recorded | deterministic | the app records gates; whether executive metrics use them is a programme check |
| ITSM-AT-05-C03 | Request Fulfillment Path Accuracy | human | requests must be tested end to end |
| ITSM-AT-09-C04 | Regulatory Control Mapping | human | regulatory control mapping and evidence of operation |
| SRE-AT-03-C05 | Executive / Leadership Dashboard Availability | human | whether a dashboard answers the 5 executive questions |
| SRE-AT-09-C05 | Incident Timeline Accuracy & Tooling | human | timeline reconstructed from logs to 5-minute resolution |

## How this was produced

- Rule coverage: `run_assessment` with every template selected, over `eval/scenarios/*` plus two rendered kit specs; a criterion counted as settled if any rule gave it a result in any run (measured before group 1 was built).
- Some of the 53 rule-settled criteria are settled only when certain evidence is present; without it they also fall to the LLM or the assessor.
- Groups were assigned by hand from each criterion's acceptance criteria, formula, data source and required evidence.
