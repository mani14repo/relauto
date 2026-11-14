"""Reliability signals in source code: a fast, deterministic, pattern-based pass (not a compiler-grade analysis).

    analyze(files: list[tuple[path, text]]) -> dict   # the "sra.code-signals/v1" evidence document

What it looks for, by category (each signal names the criteria it informs; the code_signals detector scores them):
- latency / resilience: outbound HTTP calls without a timeout (Python requests/urllib, Go's default client, Java
  HttpURLConnection/HttpClient/RestTemplate, axios, fetch), blocking calls inside async functions;
- security: hard-coded credentials, TLS verification turned off, SQL built from strings, eval/exec, unsafe
  deserialization (pickle, yaml.load, ObjectInputStream), shell=True, debug mode on, wildcard CORS, weak hashes;
- performance: database or HTTP calls inside loops (N+1), sequential awaits in loops, SELECT *;
- scalability: unbounded thread pools and queues, process-local caches and in-memory sessions, connections opened
  per call without a pool;
- coding practice / observability: swallowed exceptions and bare except, print/console logging, no tests, TODO/FIXME;
- capabilities found (positive evidence): timeouts, retries with backoff, circuit breakers, rate limiting, structured
  logging, metrics, tracing, health endpoints, graceful shutdown, caching, connection pooling.

The raw code is never kept: a signal carries the path, the line number and at most a short, redacted snippet (no
snippet at all for credentials). Patterns favour precision; every result the detector derives asks for human review.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath

from .redact import SECRETS, redact

FORMAT = "sra.code-signals/v1"
VERSION = 1
MAX_PER_RULE = 50                       # occurrences kept per rule (all are counted)

LANG = {".py": "python", ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala", ".go": "go",
        ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript",
        ".tsx": "typescript", ".cs": "csharp", ".rb": "ruby", ".php": "php", ".rs": "rust"}
JS = {"javascript", "typescript"}
JVM = {"java", "kotlin", "scala"}
BRACES = JS | JVM | {"go", "csharp", "php", "rust"}

TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|specs|testing|e2e|it)/|(^|/)test_[^/]+\.py$|_test\.(py|go)$|"
                       r"\.(test|spec)\.[jt]sx?$|(Test|Tests|IT|Spec)\.(java|kt|scala|cs)$", re.I)

# criteria each category informs
CRIT = {
    "timeout": ["SRE-AT-05-C01", "SRE-AT-13-C03"],
    "async_block": ["SRE-AT-06-C02", "SRE-AT-05-C01"],
    "secret": ["SRE-AT-11-C02"],
    "security": ["SRE-AT-11-C01"],
    "performance": ["SRE-AT-06-C02"],
    "scalability": ["SRE-AT-06-C04", "SRE-AT-06-C05"],
    "error_handling": ["SRE-AT-13-C03", "SRE-AT-05-C05"],
    "logging": ["SRE-AT-03-C01"],
    "testing": ["SRE-AT-12-C02"],
}


@dataclass(frozen=True)
class Rule:
    id: str
    category: str               # latency, security, performance, scalability, practice, observability
    severity: str               # Critical, High, Medium, Low
    title: str
    langs: frozenset
    pattern: re.Pattern
    criteria: tuple
    action: str
    unless: re.Pattern | None = None       # negates a hit when found in the call (the line and the next few)
    file_unless: re.Pattern | None = None  # negates every hit in a file where this is found
    window: int = 5
    strings: bool = False                  # match string contents too (SQL, CORS); otherwise code outside strings only
    file_requires: re.Pattern | None = None  # only in files where this is found (e.g. server-side JavaScript)


def _r(pat: str, flags=0) -> re.Pattern:
    return re.compile(pat, flags)


ALL_LANGS = frozenset(LANG.values())
RULES: list[Rule] = [
    # ---- latency / resilience
    Rule("py-requests-no-timeout", "latency", "High", "HTTP call without a timeout (requests)", frozenset({"python"}),
         _r(r"\brequests\.(get|post|put|patch|delete|head|request)\("), tuple(CRIT["timeout"]),
         "Pass timeout=(connect, read) on every requests call; a hung dependency otherwise holds the worker forever",
         unless=_r(r"timeout\s*="), window=6),
    Rule("py-urlopen-no-timeout", "latency", "Medium", "urlopen without a timeout", frozenset({"python"}),
         _r(r"\burlopen\("), tuple(CRIT["timeout"]), "Pass timeout= to urlopen", unless=_r(r"timeout\s*="), window=3),
    Rule("go-default-http-client", "latency", "High", "Go default HTTP client (no timeout)", frozenset({"go"}),
         _r(r"\bhttp\.(Get|Post|PostForm|Head)\(|\bhttp\.DefaultClient\b"), tuple(CRIT["timeout"]),
         "Use an http.Client with Timeout set (the default client never times out)", window=1),
    Rule("go-client-no-timeout", "latency", "Medium", "http.Client without Timeout", frozenset({"go"}),
         _r(r"&?http\.Client\{"), tuple(CRIT["timeout"]), "Set Timeout on the http.Client",
         unless=_r(r"Timeout\s*:"), window=6),
    Rule("java-urlconnection-no-timeout", "latency", "Medium", "HttpURLConnection without connect/read timeouts",
         frozenset(JVM), _r(r"\.openConnection\(\)"), tuple(CRIT["timeout"]),
         "Call setConnectTimeout and setReadTimeout", file_unless=_r(r"setReadTimeout|setConnectTimeout")),
    Rule("java-httpclient-no-timeout", "latency", "Medium", "Java HttpClient without a connect timeout", frozenset(JVM),
         _r(r"HttpClient\.newHttpClient\(\)"), tuple(CRIT["timeout"]),
         "Build the client with connectTimeout(...) and set a request timeout", window=1),
    Rule("java-resttemplate-no-timeout", "latency", "Medium", "RestTemplate without timeouts", frozenset(JVM),
         _r(r"new\s+RestTemplate\(\s*\)"), tuple(CRIT["timeout"]),
         "Create RestTemplate through RestTemplateBuilder with setConnectTimeout/setReadTimeout",
         file_unless=_r(r"setReadTimeout|setConnectTimeout|ReadTimeout|connectTimeout")),
    Rule("js-axios-no-timeout", "latency", "Medium", "axios client without a timeout", frozenset(JS),
         _r(r"\baxios\.create\("), tuple(CRIT["timeout"]), "Set timeout on the axios instance",
         unless=_r(r"timeout\s*:"), window=8),
    Rule("js-fetch-no-timeout", "latency", "Low", "fetch without an abort signal or timeout", frozenset(JS),
         _r(r"(?<![\w.])fetch\("), tuple(CRIT["timeout"]),
         "Pass signal: AbortSignal.timeout(ms) to fetch for calls to other services",
         file_unless=_r(r"AbortSignal|AbortController|signal\s*:|timeout"),
         file_requires=_r(r"process\.env|require\(|from\s+['\"](node:|express|undici|node-fetch|next/server)")),

    # ---- security
    Rule("tls-verify-off", "security", "High", "TLS certificate verification turned off", ALL_LANGS,
         _r(r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false|NODE_TLS_REJECT_UNAUTHORIZED\s*=|"
            r"InsecureSkipVerify\s*:\s*true|NoopHostnameVerifier|ALLOW_ALL_HOSTNAME_VERIFIER|"
            r"ServerCertificateCustomValidationCallback\s*=.*=>\s*true|CERT_NONE"), tuple(CRIT["security"]),
         "Keep certificate verification on; trust the internal CA bundle instead", window=1),
    Rule("sql-string-build", "security", "High", "SQL built from strings (injection risk)", ALL_LANGS,
         _r(r"""(execute|executemany|executeQuery|executeUpdate|query|raw)\s*\(\s*(f["'][^"']*\b(SELECT|INSERT|UPDATE|DELETE)\b[^"']*\{|["'][^"']*\b(SELECT|INSERT|UPDATE|DELETE)\b[^"']*["']\s*(\+|%|\.format))|"""
            r"""(execute|query)\s*\(\s*`[^`]*\b(SELECT|INSERT|UPDATE|DELETE)\b[^`]*\$\{""", re.I),
         tuple(CRIT["security"]), "Use parameterised queries (placeholders and bound values)", window=1, strings=True),
    Rule("eval-exec", "security", "Medium", "Dynamic code execution (eval/exec)", frozenset({"python", "php", "ruby"}),
         _r(r"(?<![\w.])(eval|exec)\s*\("), tuple(CRIT["security"]),
         "Remove eval/exec on data; parse it instead (json, ast.literal_eval)", window=1),
    Rule("js-eval", "security", "High", "Dynamic code execution (eval / new Function)", frozenset(JS),
         _r(r"(?<![\w.])eval\s*\(|new\s+Function\s*\("), tuple(CRIT["security"]),
         "Never eval request data; parse it (JSON.parse, Number) and validate it", window=1),
    Rule("unsafe-deserialization", "security", "High", "Unsafe deserialization", ALL_LANGS,
         _r(r"\bpickle\.loads?\(|\bcPickle\.loads?\(|\bmarshal\.loads?\(|new\s+ObjectInputStream\(|BinaryFormatter\(\)|"
            r"\bunserialize\("), tuple(CRIT["security"]),
         "Do not deserialize untrusted data with pickle/ObjectInputStream; use JSON or a schema-checked format", window=1),
    Rule("yaml-unsafe-load", "security", "Medium", "yaml.load without a safe loader", frozenset({"python"}),
         _r(r"\byaml\.load\("), tuple(CRIT["security"]), "Use yaml.safe_load",
         unless=_r(r"SafeLoader|CSafeLoader|BaseLoader"), window=2),
    Rule("shell-true", "security", "Medium", "Subprocess with shell=True", frozenset({"python"}),
         _r(r"shell\s*=\s*True"), tuple(CRIT["security"]), "Pass an argument list without shell=True", window=1),
    Rule("debug-on", "security", "Medium", "Debug mode enabled in code", frozenset({"python"} | JS),
         _r(r"\.run\([^)]*debug\s*=\s*True|^\s*DEBUG\s*=\s*True\b"), tuple(CRIT["security"]),
         "Turn debug off outside development (it exposes stack traces and, in Flask, a code console)", window=1),
    Rule("cors-wildcard", "security", "Low", "CORS allows any origin", ALL_LANGS,
         _r(r"Access-Control-Allow-Origin['\"]?\s*[,:]\s*['\"]\*|allow_origins\s*=\s*\[\s*['\"]\*|origin\s*:\s*['\"]\*['\"]|"
            r"AllowAnyOrigin\(\)|@CrossOrigin\(\s*\)|@CrossOrigin\(origins\s*=\s*\"\*\""), tuple(CRIT["security"]),
         "List the allowed origins", window=1, strings=True),
    Rule("weak-hash", "security", "Low", "MD5/SHA-1 in use", ALL_LANGS,
         _r(r"hashlib\.(md5|sha1)\(|MessageDigest\.getInstance\(\s*\"(MD5|SHA-?1)\"|createHash\(\s*['\"](md5|sha1)['\"]|"
            r"\bmd5\.New\(|\bsha1\.New\("), tuple(CRIT["security"]),
         "Use SHA-256 or better where the hash protects anything (passwords: bcrypt/argon2)", window=1),

    # ---- performance
    Rule("select-star", "performance", "Low", "SELECT * in a query", ALL_LANGS,
         _r(r"""["'`]\s*SELECT\s+\*\s+FROM""", re.I), tuple(CRIT["performance"]),
         "Select only the columns needed", window=1, strings=True),

    # ---- scalability
    Rule("java-unbounded-pool", "scalability", "Medium", "Unbounded thread pool", frozenset(JVM),
         _r(r"Executors\.newCachedThreadPool\(|new\s+LinkedBlockingQueue<[^>]*>\(\s*\)"), tuple(CRIT["scalability"]),
         "Use a bounded pool and queue so load sheds instead of exhausting memory and threads", window=1),
    Rule("py-unbounded-queue", "scalability", "Low", "Unbounded in-process queue", frozenset({"python"}),
         _r(r"\b(queue\.)?(Queue|SimpleQueue)\(\s*\)"), tuple(CRIT["scalability"]),
         "Give the queue a maxsize so producers get back-pressure", window=1),
    Rule("memory-session-store", "scalability", "Medium", "In-memory session store", frozenset(JS),
         _r(r"\bsession\(\s*\{"), tuple(CRIT["scalability"]),
         "Keep sessions in a shared store (Redis, database) so any replica can serve a user",
         file_unless=_r(r"store\s*:")),
    Rule("connect-per-call", "scalability", "Low", "Database connection opened directly (no pool)", ALL_LANGS,
         _r(r"\b(psycopg2?|pymysql|mysql\.connector|sqlite3|cx_Oracle|pyodbc)\.connect\(|DriverManager\.getConnection\("),
         tuple(CRIT["scalability"]), "Use a connection pool sized for the replica count",
         file_unless=_r(r"pool|Pool|HikariDataSource", 0)),

    # ---- coding practice
    Rule("bare-except", "practice", "Medium", "Bare except (catches everything)", frozenset({"python"}),
         _r(r"^\s*except\s*:"), tuple(CRIT["error_handling"]),
         "Catch the specific exceptions and log the rest with context", window=1),
]

# swallowed exceptions are found on the whole text (they span lines)
# truly empty handlers: one with a comment ("ignore: best effort") is a deliberate choice
SWALLOW_BRACE = re.compile(r"catch\s*(\([^)]*\))?\s*\{\s*\}|if\s+err\s*!=\s*nil\s*\{\s*\}")
PRINT_LOG = {"python": re.compile(r"^\s*print\("), "javascript": re.compile(r"\bconsole\.(log|debug)\("),
             "typescript": re.compile(r"\bconsole\.(log|debug)\("), "java": re.compile(r"System\.(out|err)\.print"),
             "go": re.compile(r"\bfmt\.Print"), "csharp": re.compile(r"Console\.Write")}
TODO = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
CODE_SECRET = re.compile(r"""(?i)\b\w*(password|passwd|pwd|secret|token|api_?key|access_?key|client_?secret)\w*["']?\s*[:=]\s*["']([^"'\s]{8,})["']""")
CONST_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")          # an environment variable or constant name, not a value
REGEX_TEXT = re.compile(r"[\[\]\\^$*+?(){}|]")
PLACEHOLDER = re.compile(r"(?i)change.?me|example|dummy|sample|placeholder|your[_-]|xxx|\*\*\*|<|\$\{|\{\{|%\(|test|fake|redacted|none|null")
SECRET_PATTERNS = [(n, p) for n, p, _ in SECRETS if n not in ("credential_assignment", "private_key")] + [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"))]

CAPABILITIES: dict[str, re.Pattern] = {
    "timeouts": _r(r"timeout\s*[=:(]|setReadTimeout|setConnectTimeout|connectTimeout|Timeout\s*:|AbortSignal\.timeout|"
                   r"context\.WithTimeout", re.I),
    "retries_with_backoff": _r(r"\btenacity\b|\bbackoff\b|Retry\(|@Retryable|resilience4j\.retry|RetryPolicy|"
                               r"Polly|retry-axios|axios-retry|p-retry|cenkalti/backoff|failsafe|urllib3\.util\.retry|"
                               r"exponential", re.I),
    "circuit_breaker": _r(r"circuit[_-]?breaker|pybreaker|CircuitBreaker|opossum|gobreaker|hystrix|"
                          r"resilience4j\.circuitbreaker|outlierDetection", re.I),
    "rate_limiting": _r(r"rate[_-]?limit|slowapi|flask[_-]limiter|express-rate-limit|bucket4j|RateLimiter|"
                        r"golang\.org/x/time/rate|throttl", re.I),
    "structured_logging": _r(r"\bstructlog\b|pythonjsonlogger|json_logger|JsonFormatter|logstash|\bpino\b|winston|"
                             r"LogstashEncoder|JsonLayout|go\.uber\.org/zap|zerolog|log/slog|Serilog|bunyan", re.I),
    "metrics": _r(r"prometheus_client|prom-client|micrometer|statsd|datadog|opentelemetry\.metrics|otel.*metric|"
                  r"MeterRegistry|promauto|prometheus/client_golang|newrelic", re.I),
    "tracing": _r(r"opentelemetry|otel|jaeger|zipkin|ddtrace|dd-trace|@WithSpan|tracer\.start", re.I),
    "health_endpoint": _r(r"""["'`](/health(z|check)?|/ready(z)?|/live(z|ness)?|/status)["'`]|actuator|HealthCheck""", re.I),
    "graceful_shutdown": _r(r"SIGTERM|signal\.Notify|registerShutdownHook|@PreDestroy|on_shutdown|lifespan|"
                            r"graceful(ly)?[_ -]?shut", re.I),
    "caching": _r(r"\bredis\b|memcache|lru_cache|@cache\b|@Cacheable|caffeine|node-cache|cachetools", re.I),
    "connection_pooling": _r(r"pool|HikariDataSource|create_engine|pgxpool|sql\.DB", re.I),
}


GENERATED = re.compile(r"(^|/)(generated|gen|__generated__|autogen)/|\.(gen|generated|pb)\.[a-z]+$|_pb2(_grpc)?\.py$|"
                       r"\.designer\.cs$|(^|/)migrations/\d", re.I)
GENERATED_MARK = re.compile(r"(?i)(auto-?generated|do not edit|code generated by|@generated)")


def language(path: str) -> str | None:
    return LANG.get(PurePosixPath(path).suffix.lower())


def is_test(path: str) -> bool:
    return bool(TEST_PATH.search(path))


def _snippet(line: str) -> str:
    return redact(line.strip())[:160]


class _Collector:
    def __init__(self):
        self.hits: dict[str, list[dict]] = defaultdict(list)
        self.count: Counter = Counter()
        self.meta: dict[str, dict] = {}

    def add(self, rule_id: str, meta: dict, path: str, line: int, snippet: str | None):
        self.count[rule_id] += 1
        self.meta.setdefault(rule_id, meta)
        if len(self.hits[rule_id]) < MAX_PER_RULE:
            self.hits[rule_id].append({"path": path, "line": line, **({"snippet": snippet} if snippet else {})})


def _meta(rule: Rule) -> dict:
    return {"category": rule.category, "severity": rule.severity, "title": rule.title, "criteria": list(rule.criteria),
            "action": rule.action}


EXTRA = {
    "hardcoded-secret": {"category": "security", "severity": "Critical", "title": "Credential hard-coded in source",
                         "criteria": CRIT["secret"],
                         "action": "Rotate the credential, move it to the secret manager, and purge it from history"},
    "swallowed-exception": {"category": "practice", "severity": "Medium", "title": "Exception swallowed (empty handler)",
                            "criteria": CRIT["error_handling"],
                            "action": "Log the error with context, count it as a metric, and decide: retry, degrade or fail"},
    "async-blocking-call": {"category": "latency", "severity": "High", "title": "Blocking call inside an async function",
                            "criteria": CRIT["async_block"],
                            "action": "Use an async client (httpx.AsyncClient, asyncio.sleep) or run the call in a thread pool"},
    "query-in-loop": {"category": "performance", "severity": "Medium", "title": "Database or HTTP call inside a loop (N+1)",
                      "criteria": CRIT["performance"], "action": "Batch the calls (one query with IN, bulk API) or prefetch"},
    "await-in-loop": {"category": "performance", "severity": "Low", "title": "Sequential await inside a loop",
                      "criteria": CRIT["performance"], "action": "Run independent calls concurrently (Promise.all with a limit)"},
    "process-local-cache": {"category": "scalability", "severity": "Low", "title": "Process-local cache or state",
                            "criteria": CRIT["scalability"],
                            "action": "Use a shared cache (Redis) if replicas must agree, and bound its size"},
}
PY_CALL_IN_LOOP = re.compile(r"\.(execute|executemany|query|filter|get|fetch\w*|find\w*|save|commit)\(|requests\.\w+\(|"
                             r"\.objects\.|session\.query\(")
PY_BLOCKING = re.compile(r"\brequests\.\w+\(|\btime\.sleep\(|\burlopen\(|\bpsycopg2?\.connect\(")
MODULE_CACHE = re.compile(r"^_?\w*(cache|CACHE|_store|sessions|SESSIONS)\w*\s*(:\s*dict[^=]*)?=\s*(\{\}|dict\(\)|\[\])\s*$")


def _views(lines: list[str], lang: str) -> tuple[list[str], list[str]]:
    """Per line: (code without comments or docstrings, the same with string contents blanked). Line numbers are kept.
    A triple-quoted string that starts a statement is a docstring and is dropped; other strings keep their text in the
    first view (SQL lives there)."""
    code, blank = [], []
    hashes = lang in ("python", "ruby")
    q = None              # open string delimiter
    doc = False           # the open string is a docstring
    comment = False       # inside /* ... */
    for line in lines:
        c, b, i = [], [], 0
        while i < len(line):
            ch = line[i]
            if comment:
                end = line.find("*/", i)
                if end < 0:
                    i = len(line)
                    break
                i, comment = end + 2, False
                continue
            if q:
                if ch == "\\":
                    piece = line[i:i + 2]
                    if not doc:
                        c.append(piece)
                        b.append(" " * len(piece))
                    i += 2
                    continue
                if line.startswith(q, i):
                    if not doc:
                        c.append(q)
                        b.append(q)
                    i += len(q)
                    q, doc = None, False
                    continue
                if not doc:
                    c.append(ch)
                    b.append(" ")
                i += 1
                continue
            if (hashes and ch == "#") or (not hashes and line.startswith("//", i)):
                break
            if not hashes and line.startswith("/*", i):
                comment = True
                i += 2
                continue
            if lang == "python" and line.startswith(('"""', "\'\'\'"), i):
                q = line[i:i + 3]
                doc = not "".join(c).strip()           # nothing before it on the line: a docstring
                if not doc:
                    c.append(q)
                    b.append(q)
                i += 3
                continue
            if ch in "\"'`":
                q = ch
                c.append(ch)
                b.append(ch)
                i += 1
                continue
            c.append(ch)
            b.append(ch)
            i += 1
        if q and len(q) == 1 and q != "`":            # an unterminated one-line string ends with the line
            q = None
        code.append("".join(c))
        blank.append("".join(b))
    return code, blank


def _python_blocks(lines: list[str], path: str, out: _Collector, original: list[str]) -> None:
    """Indentation scopes: blocking calls inside async def, calls inside for/while loops, module-level caches."""
    stack: list[tuple[int, str]] = []         # (indent, "async"|"def"|"loop")
    for n, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        ind = len(raw) - len(raw.lstrip())
        while stack and ind <= stack[-1][0]:
            stack.pop()
        kinds = {k for _, k in stack}
        nearest_def = next((k for _, k in reversed(stack) if k in ("async", "def")), None)
        if nearest_def == "async" and PY_BLOCKING.search(s) and "await" not in s and "to_thread" not in s:
            out.add("async-blocking-call", EXTRA["async-blocking-call"], path, n, _snippet(original[n - 1]))
        if "loop" in kinds and PY_CALL_IN_LOOP.search(s) and not s.startswith(("for ", "while ")) and "dict.get" not in s \
                and not re.search(r"\b(os\.environ|kwargs|args|params|headers|data|row|item|cfg|config|self\.\w+)\.get\(", s):
            if re.search(r"\.(execute|executemany|query)\(|requests\.\w+\(|\.objects\.|session\.query\(", s):
                out.add("query-in-loop", EXTRA["query-in-loop"], path, n, _snippet(original[n - 1]))
        if ind == 0 and MODULE_CACHE.match(s):
            out.add("process-local-cache", EXTRA["process-local-cache"], path, n, _snippet(original[n - 1]))
        if s.endswith(":"):
            if s.startswith("async def "):
                stack.append((ind, "async"))
            elif s.startswith("def "):
                stack.append((ind, "def"))
            elif s.startswith(("for ", "async for ")):
                stack.append((ind, "loop"))
        if re.match(r"except\s*(\(?\s*(Base)?Exception\b[^:]*)?:\s*$", s):   # broad handler whose body is only pass
            nxt = next((x.strip() for x in lines[n:n + 3] if x.strip()), "")
            if nxt in ("pass", "..."):
                out.add("swallowed-exception", EXTRA["swallowed-exception"], path, n, _snippet(original[n - 1]))


def _looks_secret(v: str) -> bool:
    return (not PLACEHOLDER.search(v) and not CONST_NAME.match(v) and not REGEX_TEXT.search(v) and "/" not in v
            and (re.search(r"\d", v) is not None or len(v) >= 20))


def _secrets(lines: list[str], path: str, out: _Collector) -> None:
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        m = CODE_SECRET.search(line)
        if m and _looks_secret(m.group(2)) and not re.search(r"(?i)getenv|environ|process\.env|config\[", line):
            out.add("hardcoded-secret", EXTRA["hardcoded-secret"], path, n, None)
            continue
        for _, pat in SECRET_PATTERNS:
            hit = pat.search(line)
            if hit and not REGEX_TEXT.search(hit.group(0).replace("://", "").replace("-----", "")):
                out.add("hardcoded-secret", EXTRA["hardcoded-secret"], path, n, None)
                break


def analyze(files: list[tuple[str, str]], repository: str = "", sha: str = "", truncated: int = 0) -> dict:
    out = _Collector()
    langs: Counter = Counter()
    caps: dict[str, list[str]] = defaultdict(list)
    tests = prints = todos = lines_total = 0
    source_files = 0
    for path, text in files:
        lang = language(path)
        if not lang:
            continue
        if is_test(path):
            tests += 1
            continue
        lines = text.splitlines()
        if (lines and max(len(x) for x in lines) > 2000) or GENERATED.search(path) \
                or GENERATED_MARK.search("\n".join(lines[:5])):   # minified or generated: not what the team wrote
            continue
        source_files += 1
        langs[lang] += 1
        lines_total += len(lines)
        code_lines, blank_lines = _views(lines, lang)
        code_text, blank_text = "\n".join(code_lines), "\n".join(blank_lines)
        for name, pat in CAPABILITIES.items():          # imports and settings: strings count, comments do not
            if len(caps[name]) < 5 and pat.search(code_text):
                caps[name].append(path)
        _secrets(code_lines, path, out)
        if lang == "python":
            _python_blocks(blank_lines, path, out, lines)
        if lang in BRACES:
            for m in SWALLOW_BRACE.finditer(text):
                first, last = text.count("\n", 0, m.start()), text.count("\n", 0, m.end())
                out.add("swallowed-exception", EXTRA["swallowed-exception"], path, first + 1,
                        _snippet(" ".join(x.strip() for x in lines[first:last + 1])))
        if lang in JS:
            for n, line in enumerate(blank_lines, 1):
                if re.search(r"^\s*for\s*\(.*\)\s*\{|\.forEach\(\s*async", line):
                    for k, nxt in enumerate(blank_lines[n:n + 6], n + 1):
                        if re.search(r"\bawait\s", nxt):
                            out.add("await-in-loop", EXTRA["await-in-loop"], path, k, _snippet(lines[k - 1]))
                            break
                        if nxt.strip().startswith("}"):
                            break
        pl = PRINT_LOG.get(lang)
        todos += sum(1 for x in lines if TODO.search(x))
        for n, (line, bline) in enumerate(zip(code_lines, blank_lines), 1):
            if not line.strip():
                continue
            if pl and pl.search(bline):
                prints += 1
            for rule in RULES:
                view = line if rule.strings else bline
                if lang not in rule.langs or not rule.pattern.search(view):
                    continue
                if rule.file_unless and rule.file_unless.search(code_text):
                    continue
                if rule.file_requires and not rule.file_requires.search(code_text):
                    continue
                if rule.unless and rule.unless.search("\n".join(code_lines[n - 1:n - 1 + rule.window])):
                    continue
                out.add(rule.id, _meta(rule), path, n, _snippet(lines[n - 1]))
    signals = [{"rule": rid, **out.meta[rid], "count": out.count[rid], "occurrences": out.hits[rid]}
               for rid in sorted(out.count, key=lambda r: ({"Critical": 0, "High": 1, "Medium": 2, "Low": 3}[out.meta[r]["severity"]], r))]
    practice = {"source_files": source_files, "test_files": tests, "print_statements": prints, "todo_comments": todos}
    return {"format": FORMAT, "version": VERSION, "repository": repository, "sha": sha, "files_scanned": source_files,
            "lines": lines_total, "languages": dict(langs), "files_not_scanned": truncated, "practice": practice,
            "capabilities": {k: v for k, v in caps.items() if v}, "signals": signals}


def summary_text(doc: dict) -> str:
    """A readable version for retrieval and the LLM-assisted criteria."""
    lines = [f"# Source code reliability signals: {doc.get('repository', '')} @ {doc.get('sha', '')[:12]}", "",
             f"{doc.get('files_scanned', 0)} source files, {doc.get('lines', 0)} lines "
             f"({', '.join(f'{k} {v}' for k, v in sorted((doc.get('languages') or {}).items()))}); "
             f"{(doc.get('practice') or {}).get('test_files', 0)} test files.", "", "## Capabilities found", ""]
    caps = doc.get("capabilities") or {}
    lines += [f"- {k.replace('_', ' ')}: {', '.join(v[:3])}" for k, v in caps.items()] or ["- none"]
    missing = [k.replace("_", " ") for k in CAPABILITIES if k not in caps]
    if missing:
        lines.append(f"- not found: {', '.join(missing)}")
    lines += ["", "## Signals", ""]
    for s in doc.get("signals") or []:
        where = ", ".join(f"{o['path']}:{o['line']}" for o in s["occurrences"][:5])
        lines.append(f"- [{s['severity']}] {s['category']}: {s['title']} ({s['count']}x) at {where}")
    if not doc.get("signals"):
        lines.append("- none")
    return "\n".join(lines) + "\n"
