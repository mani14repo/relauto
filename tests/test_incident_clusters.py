"""Recurring incidents grouped by text similarity (incident_clusters): advisory findings and review flags only."""
import copy
from datetime import datetime, timedelta

import pytest

from sra.catalog.models import Result
from sra.config import load_yaml
from sra.detectors import incident_clusters as ic
from sra.detectors.base import Context, DetectorReport, result
from sra.models import ApplicationRef, Assessment, FindingType
from sra.pipeline import run_detectors

AS_OF = datetime(2026, 11, 4, 9)
QUEUE = ["orders-api message queue backlog growing", "Message backlog building up in the orders-api queue",
         "Orders queue consumer stopped, message backlog", "Queue backlog: Orders messages not consumed"]
ROUTINE = ["Orders slow response reported by users", "orders-api pod restarted by Kubernetes",
           "Orders login errors for a subset of users", "orders-api-db replication lag alert",
           "Password reset emails not delivered", "orders-api DNS resolution failures"]


class Bundle:
    """The parts of an EvidenceBundle the detectors and run_detectors use."""
    items: dict = {}
    errors: list = []

    def __init__(self, rows):
        self.rows = rows

    def has_table(self, name):
        return name == "incident"

    def table(self, name):
        return self.rows if name == "incident" else []


def rows(texts, problem="", days=None, cats=None):
    out = []
    for i, t in enumerate(texts):
        out.append({"number": f"INC{len(t) * 100 + i:07d}", "short_description": t, "_evidence": "EV1", "problem_id": problem,
                    "category": (cats or ["Application", "Software", "Network", "Inquiry"])[i % 4], "subcategory": "",
                    "_opened_at": AS_OF - timedelta(days=(days or [5 + 17 * i for i in range(len(texts))])[i])})
    return out


def ctx(method="tfidf", **over):
    cfg = copy.deepcopy(load_yaml("detectors"))      # load_yaml is cached; never change the shared copy
    cfg["incident_clusters"] = {**cfg["incident_clusters"], "method": method, **over}
    return Context(app_name="Orders", aliases=["Orders Support"], as_of=AS_OF, cfg=cfg, tier=2)


def test_application_name_is_removed_but_what_failed_is_kept():
    p = ic.name_pattern(["Orders", "Orders Support"])
    text = lambda s: ic.incident_text({"short_description": s}, ["short_description"], p)  # noqa: E731
    assert text("orders-api-db disk almost full") == "db disk almost full"
    assert text("Messages stuck in the orders-api queue") == "Messages stuck in the queue"
    assert text("INC0012345 Orders batch failed on 2026-01-02") == "batch failed on"   # numbers, ids and dates go
    assert text("Support portal down") == "Support portal down"                     # generic words of the name stay


def test_clustering_is_deterministic_and_respects_the_threshold():
    v = ic.tfidf_vectors(QUEUE + ROUTINE)
    a, b = ic.leader_clusters(v, 0.35), ic.leader_clusters(v, 0.35)
    assert [(c.seed, c.members) for c in a] == [(c.seed, c.members) for c in b]
    assert all(len(c.members) == 1 for c in ic.leader_clusters(v, 0.999))   # nothing is that close


def test_reworded_recurrence_without_a_problem_is_reported_for_review():
    rep = ic.run(Bundle(rows(QUEUE + ROUTINE)), ctx())
    assert len(rep.findings) == 1, rep.status
    f = rep.findings[0]
    assert f.type == FindingType.ANOMALY and f.detector == "incident_clusters:recurrence"
    assert "similar incidents" in f.title and f.criterion_ids == ["ITSM-AT-03-C01"] and f.rubric_ids == ["P2-02"]
    assert f.confidence < 1 and "Advisory" in f.detail and "TF-IDF" in f.detail
    assert {r.split(":")[1] for r in f.evidence_refs} >= {r["number"] for r in rows(QUEUE)[:3]}
    assert "ITSM-AT-03-C01" in rep.review and not rep.results and not rep.rubric   # never sets a result


@pytest.mark.parametrize("case", ["problem", "one_wording", "two_wordings", "one_day", "routine_only"])
def test_what_the_rules_already_see_or_noise_is_not_reported(case):
    data = {
        "problem": rows(QUEUE, problem="PRB001") + rows(ROUTINE),                    # already under problem management
        "one_wording": rows([QUEUE[0]] * 4 + ROUTINE),                               # the exact-wording rule's job
        "two_wordings": rows([QUEUE[0], QUEUE[1]] * 2 + ROUTINE),                    # usually two routine issues
        "one_day": rows(QUEUE, days=[3, 3, 3, 3]) + rows(ROUTINE),                    # an outage, not recurrence
        "routine_only": rows(ROUTINE * 2),
    }[case]
    rep = ic.run(Bundle(data), ctx())
    assert not rep.findings and not rep.review, rep.status


def test_disabled_and_missing_export():
    assert ic.run(Bundle([]), ctx(enabled=False)).status.startswith("skipped: disabled")
    assert ic.run(type("B", (), {"has_table": lambda s, n: False})(), ctx()).status.startswith("skipped: no incident")


def test_review_flag_never_changes_a_result(monkeypatch):
    def flagging(b, c):
        rep = DetectorReport("flagging")
        rep.review["ITSM-AT-03-C01"] = "please look"
        return rep

    def setting(b, c):
        rep = DetectorReport("setting")
        rep.results["ITSM-AT-03-C01"] = result("ITSM-AT-03-C01", Result.MET, "no recurring patterns")
        return rep

    from sra import pipeline
    monkeypatch.setattr(pipeline, "DETECTORS", {"setting": setting, "flagging": flagging})
    a = Assessment(application=ApplicationRef(name="Orders"), templates=["ITSM-AT-03"])
    run_detectors(a, Bundle([]), ctx())
    r = a.results["ITSM-AT-03-C01"]
    assert r.result == Result.MET and r.needs_human_review and "Review: please look" in r.rationale


def test_embeddings_group_rewordings_that_share_no_words():
    pytest.importorskip("fastembed")
    try:
        ic.vectors(["probe"], "embedding")
    except RuntimeError as exc:
        pytest.skip(f"embedding model not available: {exc}")
    cert = ["TLS certificate expired on the public endpoint", "SSL cert expiry caused handshake failures",
            "Clients rejected: certificate has expired"]
    assert not ic.run(Bundle(rows(cert + ROUTINE)), ctx("tfidf")).findings          # words alone cannot see it
    rep = ic.run(Bundle(rows(cert + ROUTINE)), ctx("embedding"))
    assert len(rep.findings) == 1 and "embeddings" in rep.findings[0].detail, rep.status
    assert not ic.run(Bundle(rows(ROUTINE * 2)), ctx("embedding")).findings
