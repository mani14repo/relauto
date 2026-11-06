from collections import Counter

import pytest

from sra import config
from sra.catalog.compiler import latest_versions
from sra.catalog.docx_reader import parse_template
from sra.catalog.models import Method, Risk
from sra.catalog.rubric import parse_rubric
from sra.crosswalk import playbooks_for, rubric_criteria_map, standards_for

FILES = sorted(config.TEMPLATES_DIR.glob("*-AT-*.docx"))
VARIANT_B = {"SRE-AT-14", "SRE-AT-15", "SRE-AT-16", "ITSM-AT-11", "ITSM-AT-12"}
CMDB_V2_COUNTS = {"CMDB-AT-01": 7, "CMDB-AT-12": 7, "CMDB-AT-13": 5}   # every other V2 CMDB template has 6


@pytest.fixture(scope="module")
def templates():
    return {t.id: t for t in latest_versions([parse_template(p) for p in FILES])}


def test_all_templates_parse(templates):
    # 28 SRE/ITSM (V1) + 13 CMDB/CSDM (V2); older CMDB-AT-10/11 V1 copies in the folder are superseded
    assert len(templates) == 41
    assert sum(len(t.criteria) for t in templates.values()) == 136 + 79


def test_variants_and_counts(templates):
    for tid, t in templates.items():
        if tid.startswith("CMDB"):
            assert t.variant == "C" and t.version == "V2", tid
            assert len(t.criteria) == CMDB_V2_COUNTS.get(tid, 6), tid
        elif tid in VARIANT_B:
            assert t.variant == "B" and len(t.criteria) == 4, tid
        else:
            assert t.variant == "A", tid
            assert len(t.criteria) == (6 if tid == "SRE-AT-01" else 5), tid


def test_v2_matches_criteria_workbook(templates):
    from sra.ooxml import xlsx_sheets
    rows = {r[0]: r for r in xlsx_sheets(config.CMDB_CRITERIA_XLSX)["Criteria"][1:] if r and r[0].startswith("CMDB-AT")}
    v2 = {c.id: c for t in templates.values() if t.variant == "C" for c in t.criteria}
    assert set(rows) == set(v2)
    for cid, c in v2.items():
        assert rows[cid][3].strip() == c.name, cid
        refs = __import__("re").findall(r"(P[12]-\d{2})( \(enabler\))?", rows[cid][6])
        assert c.rubric_refs == [r for r, e in refs if not e] and c.rubric_enablers == [r for r, e in refs if e], cid


def test_criterion_fields(templates):
    for t in templates.values():
        for i, c in enumerate(t.criteria, 1):
            assert c.id == f"{t.id}-C{i:02d}"
            assert c.name and c.acceptance_criteria and c.required_evidence, c.id
            assert c.methods
            if t.variant == "B":
                assert c.default_risk_assumed and not c.example_recommendation
            elif t.variant == "C":
                assert c.formula and c.maturity_bands and c.playbooks and not c.default_risk_assumed, c.id
            else:
                assert not c.default_risk_assumed and c.example_risk, c.id


def test_sre_at_01_details(templates):
    t = templates["SRE-AT-01"]
    assert t.priority == "P1" and t.domain == "SRE"
    assert t.signoff_roles == ["Assessment Lead", "SRE Reviewer", "Engineering Owner", "PSE Representative"]
    c1 = t.criteria[0]
    assert c1.name == "SLI/SLO Definition" and c1.default_risk == Risk.HIGH and c1.methods == [Method.DETERMINISTIC]
    assert t.criteria[5].default_risk == Risk.CRITICAL


def test_method_mix(templates):
    mix = Counter(m for t in templates.values() for c in t.criteria for m in c.methods)
    assert mix[Method.DETERMINISTIC] >= 110 and mix[Method.HUMAN] >= 49 and mix[Method.AI] >= 22


def test_rubric():
    r = parse_rubric(config.RUBRIC_DOCX, config.EVIDENCE_XLSX)
    assert [c.id for c in r.criteria] == [f"P1-0{i}" for i in range(1, 9)] + [f"P2-{i:02d}" for i in range(1, 11)]
    assert r.domain_weights == {"SRE": 0.5, "ITSM": 0.3, "CMDB": 0.2}
    assert [b.rating for b in r.ratings] == ["Not Ready", "At Risk", "Conditional", "Ready"]
    assert all(c.rule and c.method for c in r.criteria), "every rubric criterion has an Evidence Catalog rule"


def test_crosswalk_ids_exist(templates):
    ids = {c.id for t in templates.values() for c in t.criteria}
    cmap = rubric_criteria_map()
    assert len(cmap) == 18
    for rid, cids in cmap.items():
        assert cids, rid
        for cid in cids:
            assert cid in ids, f"{rid} -> unknown criterion {cid}"


def test_every_criterion_has_playbook_and_standard(templates):
    pb = {p.name.split(" — ")[0] for p in config.PLAYBOOKS_DIR.glob("*-PB-*.docx")}
    assert {f"CMDB-PB-{i:02d}" for i in range(1, 12)} <= pb
    std = {p.name.split(" — ")[0] for p in config.STANDARDS_DIR.glob("*-*-*.docx")}
    for t in templates.values():
        for c in t.criteria:
            assert playbooks_for(c.id), c.id
            assert standards_for(c.id), c.id
            for p in playbooks_for(c.id):
                assert p in pb, f"{c.id}: unknown playbook {p}"
            for s in standards_for(c.id):
                assert s in std, f"{c.id}: unknown standard {s}"
