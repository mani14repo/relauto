"""Registers (CSV/XLSX), the extra ServiceNow tables, and the rules on them (sra.detectors.data_rules)."""
import copy
import io
from datetime import datetime

import pytest
from openpyxl import Workbook

from sra.config import load_yaml
from sra.detectors import data_rules, rubric_rules
from sra.detectors.base import Context, DetectorReport
from sra.evidence.bundle import EvidenceBundle
from sra.evidence.registers import register_type
from sra.evidence.servicenow import guess_table

AS_OF = datetime(2026, 11, 4, 9)


def ctx(tier=2):
    return Context(app_name="Orders", aliases=[], as_of=AS_OF, cfg=copy.deepcopy(load_yaml("detectors")), tier=tier)


def xlsx(rows, title=None) -> bytes:
    wb = Workbook()
    ws = wb.active
    if title:
        ws.append([title])
    ws.append(list(rows[0]))
    for r in rows:
        ws.append(list(r.values()))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv(rows) -> bytes:
    head = list(rows[0])
    return ("\n".join([",".join(head)] + [",".join(str(r[h]) for h in head) for r in rows]) + "\n").encode()


# ---------------------------------------------------------------- recognising files
@pytest.mark.parametrize("name, want", [
    ("Orders_Risk_Register.xlsx", "risk"), ("risks.csv", "risk"), ("policy_exceptions.csv", "exception"),
    ("IO register.xlsx", "improvement"), ("vendor inventory.csv", "vendor"), ("contract_register.csv", "contract"),
    ("contracts.xlsx", "contract"), ("technical_debt.csv", "debt"), ("postmortem_actions.csv", "postmortem_action"),
    ("corrective_actions.csv", "corrective_action"), ("sla_breach_register.csv", "sla_breach"),
    ("contract_sla.csv", None), ("sys_audit.csv", None), ("incident.csv", None), ("change_request.csv", None),
])
def test_register_recognised_by_name(name, want):
    assert register_type(name) == want


@pytest.mark.parametrize("name, want", [
    ("cmdb_ci_service_auto.csv", "cmdb_ci_service_auto"), ("cmdb_ci_linux_server.csv", "cmdb_ci"),
    ("export_cmdb_rel_ci_model.csv", "cmdb_rel_ci"), ("task_cmdb_ci_service.csv", "task_cmdb_ci_service"),
    ("task_ci.csv", "task_ci"), ("contract_sla.csv", "contract_sla"), ("task_sla.csv", "task_sla"),
])
def test_servicenow_tables_matched_longest_first(name, want):
    assert guess_table(name, []) == want


def test_xlsx_register_with_title_row_and_odd_headers():
    data = xlsx([{"Risk ID": "R-1", "Risk Description": "Single region", "Probability": "High", "Impact": "High",
                  "Mitigation Plan": "Replica", "Risk Owner": "Team A", "Target Date": datetime(2027, 1, 31), "Application": "Orders"}],
                title="Orders risk register (Q4)")
    b = EvidenceBundle()
    it = b.ingest("Orders Risk Register.xlsx", data, as_of=AS_OF)
    assert it.table == "register:risk" and it.meta["register"] == "risk"
    r = b.table("register:risk")[0]
    assert r["likelihood"] == "High" and r["owner"] == "Team A" and r["mitigation"] == "Replica"
    assert r["_target_date"].date().isoformat() == "2027-01-31"


def test_hint_names_the_register_for_any_file_name():
    b = EvidenceBundle()
    it = b.ingest("export (3).csv", csv([{"Vendor": "CloudCo", "Services": "DB", "Applications": "Orders"}]), hint="register:vendor",
                  as_of=AS_OF)
    assert it.table == "register:vendor" and b.table("register:vendor")[0]["vendor"] == "CloudCo"


def test_xlsx_servicenow_export():
    b = EvidenceBundle()
    it = b.ingest("cmdb_ci_service_auto.xlsx", xlsx([{"name": "Orders - Production", "environment": "Production"}]), as_of=AS_OF)
    assert it.table == "cmdb_ci_service_auto" and b.table("cmdb_ci_service_auto")[0]["name"] == "Orders - Production"


# ---------------------------------------------------------------- rule behaviour
def test_rules_need_their_data():
    assert data_rules.run(EvidenceBundle(), ctx()).results == {}


def test_hosting_cis_are_not_the_business_application():
    b = EvidenceBundle()
    b.ingest("cmdb_ci.csv", csv([{"name": "Orders", "sys_class_name": "cmdb_ci_business_app", "owned_by": "x"},
                                 {"name": "orders-vm01", "sys_class_name": "cmdb_ci_linux_server", "owned_by": ""}]), as_of=AS_OF)
    assert [r["name"] for r in rubric_rules._app_ci(b, ctx())] == ["Orders"]


def test_service_model_links_are_not_dependencies():
    b = EvidenceBundle()
    b.ingest("cmdb_ci_service_auto.csv", csv([{"name": "Orders - Production", "environment": "Production"}]), as_of=AS_OF)
    b.ingest("cmdb_rel_ci.csv", csv([{"sys_id": "1", "parent": "Orders", "child": "Orders - Production",
                                      "type": "Consumes::Consumed by", "sys_updated_on": "2026-10-01 00:00:00"}]), as_of=AS_OF)
    rep = DetectorReport("t")
    rubric_rules.p2_01(b, ctx(), rep)
    assert rep.rubric["P2-01"].result.value == "Gap" and "No upstream/downstream" in rep.rubric["P2-01"].rationale


def test_quarter_bounds():
    assert data_rules.quarter_bounds(datetime(2026, 11, 4)) == (datetime(2026, 10, 1), datetime(2027, 1, 1))
    assert data_rules.quarter_bounds(datetime(2026, 11, 4), back=1) == (datetime(2026, 7, 1), datetime(2026, 10, 1))
    assert data_rules.quarter_bounds(datetime(2026, 2, 1), back=1) == (datetime(2025, 10, 1), datetime(2026, 1, 1))


def test_migration_still_inside_its_window_is_not_late():
    b = EvidenceBundle()
    b.ingest("knowledge_migration_tracker.csv", csv([
        {"Procedure": "a", "Identified Date": "2026-10-20", "Migrated Date": "", "Application": "Orders"},     # 15 days in
        {"Procedure": "b", "Identified Date": "2026-07-01", "Migrated Date": "2026-07-20", "Application": "Orders"}]), as_of=AS_OF)
    r = data_rules.run(b, ctx()).results["ITSM-AT-07-C03"]
    assert r.result.value == "Met" and "1 still inside" in r.rationale


def test_v2_results_carry_measured_value_and_level():
    b = EvidenceBundle()
    b.ingest("cmdb_ci_service_auto.csv", csv([{"name": "Orders - Production", "environment": "Production",
                                               "attested_date": "2024-01-01"}]), as_of=AS_OF)
    r = data_rules.run(b, ctx(tier=1)).results["CMDB-AT-11-C01"]
    assert r.result.value == "Gap" and r.measured_value and r.maturity_level == 0


def test_field_names_can_be_overridden():
    c = ctx()
    c.cfg["data_rules"] = {"fields": {"attested_date": "u_last_attested"}}
    b = EvidenceBundle()
    b.ingest("cmdb_ci_service_auto.csv", csv([{"name": "Orders - Production", "environment": "Production",
                                               "u_last_attested": "2026-10-01"}]), as_of=AS_OF)
    assert data_rules.run(b, c).results["CMDB-AT-11-C01"].result.value == "Met"
