"""Source code reliability signals (evidence/code_signals.py) and the code_signals detector."""
import json
from datetime import datetime

import pytest

from sra.detectors import ALL
from sra.detectors.base import Context
from sra.evidence.bundle import EvidenceBundle
from sra.evidence.code_signals import FORMAT, analyze
from sra.pipeline import load_yaml

FAKE_KEY = "sk_live_" + "9" * 24

BAD = {
    "app/client.py": f'''
import requests, pickle, yaml, subprocess, hashlib, time
API_KEY = "{FAKE_KEY}"

def get_user(uid):
    return requests.get(f"https://users.internal/u/{{uid}}")

async def handler(req):
    r = requests.post("https://billing.internal/charge", json=req)
    time.sleep(2)
    return r

def report(cur, ids):
    cur.execute(f"SELECT * FROM orders WHERE id = {{ids[0]}}")
    for i in ids:
        cur.execute("SELECT total FROM orders WHERE id = %s", (i,))
    data = pickle.loads(open("x", "rb").read())
    cfg = yaml.load(open("c.yml"))
    subprocess.run("ls " + ids[0], shell=True)
    requests.get("https://x.internal", verify=False, timeout=3)
    try:
        risky()
    except Exception:
        pass
    try:
        risky()
    except:
        log()
    return hashlib.md5(b"x").hexdigest()

_cache = {{}}
''',
    "svc/main.go": '''package main
import "net/http"
func call() {
    resp, err := http.Get("https://inventory.internal/items")
    if err != nil {}
    _ = resp
}
''',
    "api/Server.java": '''class Server {
  void run() throws Exception {
    java.util.concurrent.ExecutorService p = java.util.concurrent.Executors.newCachedThreadPool();
    java.net.HttpURLConnection c = (java.net.HttpURLConnection) new java.net.URL("https://a").openConnection();
    java.sql.Statement s = conn.createStatement();
    s.executeQuery("SELECT name FROM users WHERE id = " + id);
    try { x(); } catch (Exception e) {}
  }
}
''',
    "web/server.js": '''const express = require("express");
const session = require("express-session");
const axios = require("axios");
const app = express();
app.use(session({ secret: process.env.S }));
const client = axios.create({ baseURL: "https://pay.internal" });
process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";
async function all(ids) {
  for (const id of ids) {
    await fetch("https://stock.internal/" + id);
  }
}
''',
}

GOOD = {
    "app/client.py": '''"""Calls the users service. Never use requests.get(url) without a timeout, or pickle.loads, or verify=False."""
import logging, os, requests, structlog
from tenacity import retry, wait_exponential
import pybreaker
from prometheus_client import Counter
API_KEY = os.environ["API_KEY"]
TOKEN_NAME = "GH_TOKEN"
password = "changeme"
log = structlog.get_logger()
breaker = pybreaker.CircuitBreaker()

@retry(wait=wait_exponential())
def get_user(uid):
    # requests.get("https://example") would block forever without a timeout
    return requests.get(f"https://users.internal/u/{uid}", timeout=(2, 5))

def health():
    return {"path": "/healthz"}

def report(cur, ids):
    cur.execute("SELECT total FROM orders WHERE id = ANY(%s)", (ids,))
    try:
        risky()
    except KeyError:
        pass
''',
    "app/test_client.py": "def test_x():\n    assert True\n",
    "web/ui.ts": 'export async function load(u: string) { const r = await fetch(u); return r.json(); }\n',
}


def _rules(doc):
    return {s["rule"]: s for s in doc["signals"]}


def test_finds_the_patterns_in_bad_code():
    doc = analyze(list(BAD.items()), "acme/orders", "a" * 40)
    got = _rules(doc)
    expected = {"py-requests-no-timeout", "async-blocking-call", "hardcoded-secret", "sql-string-build", "query-in-loop",
                "unsafe-deserialization", "yaml-unsafe-load", "shell-true", "tls-verify-off", "swallowed-exception",
                "bare-except", "weak-hash", "process-local-cache", "select-star", "go-default-http-client",
                "java-unbounded-pool", "java-urlconnection-no-timeout", "memory-session-store", "js-axios-no-timeout",
                "await-in-loop", "js-fetch-no-timeout"}
    assert expected <= set(got), expected - set(got)
    assert got["py-requests-no-timeout"]["count"] == 2                 # the call with timeout=3 is not counted
    assert got["swallowed-exception"]["count"] == 3                    # python except Exception: pass, Go and Java
    assert {o["path"] for o in got["sql-string-build"]["occurrences"]} == {"app/client.py", "api/Server.java"}
    assert got["tls-verify-off"]["count"] == 2                         # verify=False and NODE_TLS_REJECT_UNAUTHORIZED
    sec = got["hardcoded-secret"]
    assert sec["severity"] == "Critical" and all("snippet" not in o for o in sec["occurrences"])
    assert FAKE_KEY not in json.dumps(doc)                              # credentials are never copied
    assert doc["languages"] == {"python": 1, "go": 1, "java": 1, "javascript": 1}
    assert doc["practice"]["test_files"] == 0


def test_safe_code_comments_and_strings_do_not_fire():
    doc = analyze(list(GOOD.items()), "acme/orders", "a" * 40)
    assert _rules(doc) == {}, [(s["rule"], s["occurrences"]) for s in doc["signals"]]
    assert {"timeouts", "retries_with_backoff", "circuit_breaker", "structured_logging", "metrics",
            "health_endpoint"} <= set(doc["capabilities"])
    assert doc["practice"] == {"source_files": 2, "test_files": 1, "print_statements": 0, "todo_comments": 0}


def test_minified_generated_and_unknown_files_are_skipped():
    doc = analyze([("dist/app.js", "x" * 3000 + "\nrequests.get('a')"), ("README.md", "requests.get(url)"),
                   ("src/client/client.gen.ts", "const i = axios.create(cfg);\n"), ("api/user_pb2.py", "x = 1\n"),
                   ("src/api.ts", "// Code generated by openapi-generator. DO NOT EDIT.\nconst i = axios.create(cfg);\n")])
    assert doc["files_scanned"] == 0 and doc["signals"] == []


def test_javascript_eval_but_not_exec():
    doc = analyze([("app/routes.js", "const x = eval(req.body.preTax);\nconst m = re.exec(s);\nexec('ls');\n")])
    got = _rules(doc)
    assert set(got) == {"js-eval"} and got["js-eval"]["severity"] == "High"


def _bundle(*docs) -> EvidenceBundle:
    b = EvidenceBundle()
    for n, d in enumerate(docs):
        b.ingest(f"github.com/acme/orders@{'a' * 12}/github-code-signals{n}.json", json.dumps(d).encode())
    return b


def _ctx():
    return Context(app_name="Orders", as_of=datetime(2026, 11, 14), cfg=load_yaml("detectors"))


def test_bundle_reads_the_document_as_code_analysis():
    b = _bundle(analyze(list(BAD.items()), "acme/orders", "a" * 40))
    (it,) = b.code_analyses()
    assert it.kind.value == "code_analysis" and it.meta["files"] == 4 and it.meta["security"] >= 8
    assert "Source code reliability signals" in it.text and FAKE_KEY not in it.text
    assert FORMAT == "sra.code-signals/v1"


def test_detector_bad_code():
    rep = ALL["code_signals"](_bundle(analyze(list(BAD.items()), "acme/orders", "a" * 40)), _ctx())
    r = rep.results
    assert r["SRE-AT-05-C01"].result.value == "Gap" and r["SRE-AT-05-C01"].needs_human_review
    assert r["SRE-AT-11-C02"].result.value == "Gap" and r["SRE-AT-11-C02"].risk.value == "Critical"
    assert r["SRE-AT-13-C03"].result.value == "Gap" and r["SRE-AT-06-C04"].result.value == "Gap"
    assert "SRE-AT-11-C01" in rep.review and "SQL built from strings" in rep.review["SRE-AT-11-C01"]
    by = {f.detector: f for f in rep.findings}
    f = by["code_signals:py-requests-no-timeout"]
    assert f.type.value == "gap" and f.severity.value == "High" and "app/client.py:6" in f.detail
    assert f.evidence_refs[0].endswith(":app/client.py:6") and "SRE-AT-05-C01" in f.criterion_ids
    assert by["code_signals:sql-string-build"].type.value == "vulnerability"
    assert by["code_signals:await-in-loop"].type.value == "observation"
    assert "code_signals:capabilities" in by


def test_detector_good_code_and_rescans_do_not_double_count():
    good = analyze(list(GOOD.items()), "acme/orders", "a" * 40)
    rep = ALL["code_signals"](_bundle(good), _ctx())
    assert {k: v.result.value for k, v in rep.results.items()} == {
        "SRE-AT-05-C01": "Met", "SRE-AT-05-C02": "Met", "SRE-AT-05-C03": "Met", "SRE-AT-03-C02": "Met", "SRE-AT-03-C01": "Met"}
    assert all(v.needs_human_review for v in rep.results.values())
    bad = analyze(list(BAD.items()), "acme/orders", "a" * 40)
    bad2 = json.loads(json.dumps(bad))
    bad2["sha"] = "b" * 40                                              # a second snapshot of the same code
    rep = ALL["code_signals"](_bundle(bad, bad2), _ctx())
    f = next(f for f in rep.findings if f.detector == "code_signals:py-requests-no-timeout")
    assert f.title.endswith("(2 places)")


def test_detector_skips_without_code():
    rep = ALL["code_signals"](EvidenceBundle(), _ctx())
    assert rep.status.startswith("skipped") and not rep.findings


@pytest.mark.parametrize("line,hit", [
    ('db_password = "s3cr3tP4ss"', True), ('token = "GH_TOKEN"', False), ('pwd = os.getenv("PWD")', False),
    ('password = "changeme"', False), ('key = "AKIA' + "ABCDEFGHIJKLMNOP" + '"', True), ('r"(://)[^:/]+:[^@]+@"', False),
])
def test_secret_heuristics(line, hit):
    assert ("hardcoded-secret" in _rules(analyze([("a.py", line + "\n")]))) is hit
