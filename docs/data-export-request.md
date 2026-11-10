# Data the assessment rules can use

The rules in `sra.detectors.data_rules` settle 52 template criteria from the ServiceNow tables and team registers below.
They run only when the export is uploaded with an assessment; otherwise the criterion is left for the assessor.

Everything here was built and tested on **synthetic data**. Field names and register headings are our best reading of
standard ServiceNow and of the templates. Before trusting the results, send a few anonymised rows of each export and
check the names:

- ServiceNow field names are overridden under `data_rules.fields` in `config/detectors.yaml`.
- Register headings are mapped in `config/registers.yaml`.

**How to export.** List view → right-click the header → Export → CSV (or Excel), or use the Table API with
`sysparm_display_value=true`. Name each file after its table, e.g. `cmdb_ci_service_auto.csv`; other `cmdb_ci_*` class
exports (servers, VMs, namespaces) are read together as `cmdb_ci`. Scope every export to the application being assessed
unless noted.

## ServiceNow tables

|                                             Table                                              |                                                              Fields the rules read                                                              | Filter                                                                               | Criteria                                                |
|:----------------------------------------------------------------------------------------------:|:-----------------------------------------------------------------------------------------------------------------------------------------------:|--------------------------------------------------------------------------------------|---------------------------------------------------------|
|                         `cmdb_ci_service_auto` (Application Services)                          |                                   name, environment, busines_criticality, u_tier_approved_by†, attested_date†                                   | services of the Business Application                                                 | CMDB-AT-01-C04, 06-C02, 10-C01, 11-C01                  |
|                                       `service_offering`                                       |                                                          name, service_classification                                                           | offerings over those services                                                        | CMDB-AT-01-C05                                          |
|                                         `cmdb_rel_ci`                                          |                                                       parent, child, type, sys_updated_on                                                       | relationships of the BA, its services and offerings (already requested)              | CMDB-AT-01-C05, P2-01                                   |
| `cmdb_ci` hosting classes (servers, VMs, DB instances, Kubernetes namespaces, cloud instances) | name, sys_class_name, environment, install_status, operational_status, support_group, used_for, discovery_source, last_discovered, duplicate_of | CIs in the application's services                                                    | CMDB-AT-02-C03/C05, 03-C03, 07-C02, 07-C04 (and 02-C04) |
|                                         `svc_ci_assoc`                                         |                                                                service_id, ci_id                                                                | the application's services                                                           | CMDB-AT-02-C05, 03-C03, 06-C02, 07-C02                  |
|                                   `reconcile_duplicate_task`                                   |                                                          number, state, sys_created_on                                                          | open and last 90 days                                                                | CMDB-AT-02-C03                                          |
|                            `discovery_schedule`, `discovery_status`                            |                                                 name, active; dscheduler, state, sys_created_on                                                 | schedules covering the application; last 30 days                                     | CMDB-AT-02-C01                                          |
|                                   `discovery_device_history`                                   |                                                                 source, status                                                                  | last 30 days, the application's hosts                                                | CMDB-AT-02-C06                                          |
|                                    `discovery_credentials`                                     |                                         name, active, credential_storage_id†, u_owner†, sys_updated_on                                          | credentials used for those hosts                                                     | CMDB-AT-02-C06                                          |
|                                       `cmdb_identifier`                                        |                                                               applies_to, active                                                                | all                                                                                  | CMDB-AT-02-C02                                          |
|                                          `sys_audit`                                           |                                                tablename, documentkey, fieldname, sys_created_on                                                | fieldname in install_status, operational_status; last 90 days; the application's CIs | CMDB-AT-03-C02                                          |
|                                           `task_ci`                                            |                                                                  task, ci_item                                                                  | changes in the last 90 days                                                          | CMDB-AT-03-C02                                          |
|                                     `task_cmdb_ci_service`                                     |                                                      task, cmdb_ci_service, sys_created_on                                                      | changes in the last 90 days                                                          | CMDB-AT-08-C02                                          |
|                                `change_request` (extra fields)                                 |                                                      type, conflict_status, approval_set†                                                       | already requested                                                                    | CMDB-AT-08-C02, 08-C06                                  |
|                                         `alm_hardware`                                         |                                                                  asset_tag, ci                                                                  | assets of the application's hardware CIs                                             | CMDB-AT-03-C04                                          |
|                                     `cmdb_sam_sw_install`                                      |                                                   installed_on, norm_product†, software_model                                                   | installs on the application's hosts                                                  | CMDB-AT-03-C06, 09-C01                                  |
|                                 `cmdb_software_product_model`                                  |                                                             name, end_of_life_date†                                                             | models in use                                                                        | CMDB-AT-09-C01                                          |
|                                    `problem` (extra fields)                                    |                                                    priority, u_root_cause_date†, resolved_at                                                    | already requested                                                                    | ITSM-AT-03-C04                                          |
|                                         `contract_sla`                                         |                                             name, collection, type, target, active, start_condition                                             | collection = incident                                                                | ITSM-AT-06-C01                                          |
|                                           `task_sla`                                           |                                                        task, task.priority, has_breached                                                        | the application's incidents, last 90 days                                            | ITSM-AT-06-C02                                          |
|                                         `sc_cat_item`                                          |                                               name, active, group, owner, u_sla_target†, cmdb_ci                                                | items for the application                                                            | ITSM-AT-05-C01, 05-C02, 05-C05                          |

† Not an out-of-box field, or one that varies between instances: confirm the name.

## Registers (spreadsheets, CSV or XLSX)

A register is recognised by its file name (e.g. `risk_register.xlsx`), or by uploading it with the hint
`register:<type>`. A title row above the table is fine. An `Application` column is used to scope rows to the
application; without one, every row counts.

|                      Register                       |                                               Columns the rules read                                                |                   Criteria                    |
|:---------------------------------------------------:|:-------------------------------------------------------------------------------------------------------------------:|:---------------------------------------------:|
|               Risk register (`risk`)                | ID, description, likelihood, impact, rating, mitigation, owner, target date, status, accepted by, acceptance expiry | ITSM-AT-09-C01, SRE-AT-02-C05, CMDB-AT-09-C04 |
|           Policy exceptions (`exception`)           |                           ID, description, approved by, expiry date, compensating control                           |        ITSM-AT-09-C05, CMDB-AT-11-C05         |
|      Improvement opportunities (`improvement`)      |   ID, title, owner, score, planned date, completed date, status, baseline metric, result metric, override reason    |             ITSM-AT-08-C01 to C04             |
|             Vendor inventory (`vendor`)             |                 vendor, services, dependent applications, critical, concentration risk, mitigation                  |            ITSM-AT-10-C01, 10-C05             |
|           Contract register (`contract`)            |                          vendor, critical, contract end, SLA, penalty clause, review date                           |                ITSM-AT-10-C02                 |
|               Technical debt (`debt`)               |                            ID, title, severity, application, opened date, completed date                            |            CMDB-AT-09-C05, 09-C06             |
|      Corrective actions (`corrective_action`)       |                                   ID, owner, due date, completed date, escalated                                    |                ITSM-AT-06-C05                 |
|      Post-mortem actions (`postmortem_action`)      |                                   ID, owner, due date, completed date, escalated                                    |                 SRE-AT-09-C03                 |
|     CMDB remediation tasks (`cmdb_remediation`)     |                                         ID, owner, due date, completed date                                         |                CMDB-AT-04-C05                 |
|          Audit findings (`audit_finding`)           |                                  ID, owner, severity, opened date, completed date                                   |                CMDB-AT-11-C03                 |
|         SLA breach register (`sla_breach`)          |                                breach date, root-cause category, improvement action                                 |                ITSM-AT-06-C04                 |
| Knowledge migration tracker (`knowledge_migration`) |                                procedure, identified date, migrated date, KB number                                 |                ITSM-AT-07-C03                 |
|                PSE roster (`roster`)                |                                       queue, agent, critical, shift, trained                                        |                ITSM-AT-04-C05                 |
|          Access reviews (`access_review`)           |                                review date, scope, least privilege, shared accounts                                 |                 SRE-AT-11-C05                 |
|         Automation registry (`automation`)          |                    name, owner, status, last activity, before hours, after hours, deployed date                     |         SRE-AT-14-C04, SRE-AT-10-C04          |
|             Toil measurements (`toil`)              |                                                   quarter, toil %                                                   |             SRE-AT-10-C02, 10-C05             |
|            Deployment log (`deployment`)            |                                         deployed at, commit at, environment                                         |                 SRE-AT-12-C04                 |

## Not covered yet (30 criteria)

These need data whose format depends on your tools, so no rule was written:

- APM and observed-traffic exports (CMDB-AT-06-C05, SRE-AT-03-C04, SRE-AT-13-C02);
- cloud account and billing exports (CMDB-AT-07-C01, 07-C06);
- the telemetry mapping store (CMDB-AT-12, CMDB-AT-01-C07);
- DBA, middleware and load-balancer inventories (CMDB-AT-05);
- CMDB Health and certification configuration (CMDB-AT-04-C01/C03, 10-C05, 11-C02);
- the FSI scope list (CMDB-AT-13-C01).

The full list is in `docs/llm-replacement-inventory.md`, group 2, where "Built" says no.
