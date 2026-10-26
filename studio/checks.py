### fixed update
"""Objective, non-LLM checks. Issues are dicts: {file, severity, problem, fix, source}."""
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ALLOWED_THIRD_PARTY = {
    "fastapi", "starlette", "uvicorn", "sqlalchemy", "pydantic", "dotenv", "httpx",
    "jwt", "bcrypt", "multipart", "backend", "typing_extensions", "anyio",
}
STDLIB = set(getattr(sys, "stdlib_module_names", set())) | {"__future__"}

SMOKE_CODE = """
import json, traceback
from fastapi.testclient import TestClient
from backend.main import app
bad = []
with TestClient(app) as c:
    r = c.get('/')
    assert r.status_code == 200, 'GET / returned %s' % r.status_code
    r = c.get('/openapi.json')
    assert r.status_code == 200, 'GET /openapi.json returned %s' % r.status_code
    paths = r.json().get('paths', {})
    routes = [[m.upper(), p] for p, ops in paths.items() for m in ops]
    print('ROUTES=' + json.dumps(routes))
    # call every parameterless GET API route without credentials: must answer < 500
    for p, ops in paths.items():
        if 'get' in ops and '{' not in p and p.startswith('/api'):
            try:
                s = c.get(p).status_code
                if s >= 500:
                    bad.append([p, 'returned HTTP %s' % s])
            except Exception:
                tb = traceback.format_exc().splitlines()
                frames = [l.strip() for l in tb if 'backend' in l and l.strip().startswith('File')]
                bad.append([p, tb[-1] + ' | ' + ' ; '.join(frames[-3:])])
print('BAD=' + json.dumps(bad))
print('SMOKE_OK')
"""

AUDIT_CODE = """
import importlib, json, sys
bad = []
for f, mod, names in json.load(open(sys.argv[1])):
    try:
        m = importlib.import_module(mod)
    except Exception as e:
        bad.append([f, "Cannot import '%s' (%s: %s)" % (mod, type(e).__name__, str(e)[:160])])
        continue
    for n in names:
        if n == '*':
            continue
        try:
            getattr(m, n)
            continue
        except Exception as e:
            reason = str(e).split(chr(10))[0][:140]
        try:
            importlib.import_module(mod + '.' + n)
        except Exception:
            bad.append([f, "'%s' cannot be imported from '%s': %s" % (n, mod, reason)])
print('AUDIT=' + json.dumps(bad))
"""

# Patterns that broke real builds. (regex, problem, fix)
BANNED = [
    (r"\bBaseSettings\b|pydantic_settings",
     "Uses pydantic BaseSettings / pydantic-settings, which is not part of the stack.",
     "Make backend/config.py plain Python: load_dotenv() then module-level constants from os.getenv; "
     "parse CORS_ORIGINS with .split(',')."),
    (r"create_async_engine|AsyncSession|async_sessionmaker|sqlalchemy\.ext\.asyncio|aiosqlite",
     "Uses async SQLAlchemy / aiosqlite, but the stack is synchronous SQLite.",
     "Use create_engine, sessionmaker/Session and plain `def` dependencies and routes."),
]

# Import direction: a module may only import modules with a LOWER number.
LAYERS = {"config": 0, "database": 1, "models": 2, "schemas": 3, "security": 4}
LAYER_TEXT = "config < database < models < schemas < security < routers < main"


def _issue(file, problem, fix=""):
    return {"file": file, "severity": "blocker", "problem": problem, "fix": fix, "source": "auto"}


def _read(project: Path, files: dict, rel: str):
    if rel in files:
        return files[rel]
    p = project / rel
    return p.read_text(encoding="utf-8") if p.exists() else None


def _top_names(tree) -> set:
    names = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            names |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in n.names}
    return names



def _layer(rel: str):
    parts = rel[:-3].split("/") if rel.endswith(".py") else []
    if len(parts) >= 2 and parts[0] == "backend":
        if parts[1] == "routers":
            return 5
        if parts[1] == "main":
            return 6
        return LAYERS.get(parts[1])
    return None


def _module_level_imports(tree):
    """Imports that run at import time (not inside functions/classes or TYPE_CHECKING)."""
    stack = list(tree.body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            yield n
        elif isinstance(n, ast.If) and "TYPE_CHECKING" not in ast.dump(n.test):
            stack += n.body + n.orelse
        elif isinstance(n, ast.Try):
            stack += n.body + n.orelse + n.finalbody + [s for h in n.handlers for s in h.body]
        elif isinstance(n, ast.With):
            stack += n.body


def _import_targets(files: dict, rel: str, tree) -> set:
    out = set()
    for n in _module_level_imports(tree):
        if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module and n.module.split(".")[0] == "backend":
            base = n.module.replace(".", "/")
            for a in n.names:
                sub = f"{base}/{a.name}.py"
                out.add(sub if sub in files else base + ".py")
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] == "backend":
                    out.add(a.name.replace(".", "/") + ".py")
    return {t for t in out if t in files and t != rel}


def architecture_checks(files: dict) -> list[dict]:
    issues, graph, trees = [], {}, {}
    for rel, text in files.items():
        if rel.startswith("backend/") and rel.endswith(".py"):
            try:
                trees[rel] = ast.parse(text)
            except SyntaxError:
                continue
    for rel, tree in trees.items():
        graph[rel] = _import_targets(files, rel, tree)

    flagged = set()
    for rel, targets in graph.items():
        for t in targets:
            a, b = _layer(rel), _layer(t)
            if a is not None and b is not None and b > a:
                flagged.add(rel)
                issues.append(_issue(rel, f"Imports '{t}' which is a higher layer. This causes circular imports "
                                          f"(allowed direction: {LAYER_TEXT}).",
                                     f"Remove the import of {t} from {rel}. Move the shared code down into a lower "
                                     "module, or do that work in backend/main.py (admin seeding belongs in the lifespan)."))
    # any remaining cycle between files
    seen, done = set(), set()

    def dfs(n, path):
        if n in done:
            return
        if n in path:
            cyc = path[path.index(n):] + [n]
            if not flagged.intersection(cyc):
                low = min(cyc[:-1], key=lambda r: (_layer(r) if _layer(r) is not None else 99, r))
                issues.append(_issue(low, "Circular import: " + " -> ".join(cyc),
                                     "Break the cycle by moving shared code into a lower-level module."))
                flagged.update(cyc)
            return
        for t in sorted(graph.get(n, ())):
            dfs(t, path + [n] if n not in path else path)
        done.add(n)

    for n in sorted(graph):
        dfs(n, [])
    return issues


def banned_pattern_checks(files: dict) -> list[dict]:
    issues = []
    for rel, text in files.items():
        if not rel.endswith(".py"):
            continue
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for pat, problem, fix in BANNED:
            if re.search(pat, code):
                issues.append(_issue(rel, problem, fix))
    return issues


def _config_names(tree) -> tuple[set, set]:
    """(module-level names, attributes of the object bound to `settings`)."""
    names = _top_names(tree)
    attrs = set()
    inst = None
    for n in tree.body:
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) \
                and any(isinstance(t, ast.Name) and t.id == "settings" for t in n.targets):
            inst = n.value.func.id
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == inst), None)
    if cls:
        for it in cls.body:
            if isinstance(it, ast.AnnAssign) and isinstance(it.target, ast.Name):
                attrs.add(it.target.id)
            elif isinstance(it, ast.Assign):
                attrs |= {t.id for t in it.targets if isinstance(t, ast.Name)}
            elif isinstance(it, (ast.FunctionDef, ast.AsyncFunctionDef)):
                attrs.add(it.name)
        for sub in ast.walk(cls):
            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == "self" \
                    and isinstance(sub.ctx, ast.Store):
                attrs.add(sub.attr)
    return names, (attrs if cls else None)


def config_usage_checks(files: dict) -> list[dict]:
    """`settings.X` / `config.X` must refer to something backend/config.py really defines."""
    cfg = files.get("backend/config.py")
    if not cfg:
        return []
    try:
        names, attrs = _config_names(ast.parse(cfg))
    except SyntaxError:
        return []
    issues = []
    for rel, text in files.items():
        if not rel.endswith(".py") or rel == "backend/config.py":
            continue
        if attrs is not None and re.search(r"from backend\.config import[^\n]*\bsettings\b", text):
            for a in sorted(set(re.findall(r"\bsettings\.([A-Za-z_]\w*)", text))):
                if a not in attrs and not a.startswith("__"):
                    issues.append(_issue(rel, f"Uses settings.{a} but backend/config.py's Settings has no '{a}'.",
                                         f"Define {a} in backend/config.py or use an existing name."))
        if re.search(r"from backend import[^\n]*\bconfig\b|import backend\.config as config", text):
            for a in sorted(set(re.findall(r"\bconfig\.([A-Za-z_]\w*)", text))):
                if a not in names:
                    issues.append(_issue(rel, f"Uses config.{a} but backend/config.py does not define '{a}'.",
                                         f"Define {a} in backend/config.py or use an existing name."))
    return issues


def static_checks(project: Path, files: dict) -> list[dict]:
    issues = []
    for req in ("backend/main.py", "frontend/index.html", "frontend/css/styles.css"):
        if req not in files:
            issues.append(_issue(req, f"Required file {req} is missing."))

    for rel, text in files.items():
        if rel.endswith(".py"):
            try:
                tree = ast.parse(text)
            except SyntaxError as e:
                issues.append(_issue(rel, f"Syntax error at line {e.lineno}: {e.msg}"))
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods = [node.module.split(".")[0]]
                else:
                    continue
                for m in mods:
                    if m not in STDLIB and m not in ALLOWED_THIRD_PARTY:
                        issues.append(_issue(rel, f"Imports disallowed library '{m}'.", "Use only the allowed libraries."))
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and mods[0] == "backend":
                    issues += _check_backend_import(project, files, rel, node)

    main = files.get("backend/main.py", "")
    if main and not re.search(r"^\s*app\s*=\s*FastAPI", main, re.M):
        issues.append(_issue("backend/main.py", "Does not define `app = FastAPI(...)`."))

    for rel, text in files.items():
        if rel.endswith(".html"):
            for ref in re.findall(r'(?:src|href)="([^"]+)"', text):
                if re.match(r"(https?:|//|data:|mailto:|tel:|javascript:|#|/api/)", ref):
                    continue
                ref = ref.split("#")[0].split("?")[0]
                if not ref:
                    continue
                target = Path("frontend") / ref.lstrip("/") if ref.startswith("/") else Path(rel).parent / ref
                norm = os.path.normpath(str(target)).replace("\\", "/")
                if _read(project, files, norm) is None and not (project / norm).exists():
                    issues.append(_issue(rel, f"References missing file '{ref}'."))
        if rel.endswith(".js"):
            issues += _check_js_imports(project, files, rel, text)
    issues += banned_pattern_checks(files)
    issues += architecture_checks(files)
    issues += config_usage_checks(files)
    return issues


def _check_backend_import(project, files, rel, node) -> list[dict]:
    out = []
    target = node.module.replace(".", "/")
    src = _read(project, files, target + ".py")
    if src is None:
        pkg = _read(project, files, target + "/__init__.py")
        if pkg is None:
            return [_issue(rel, f"Imports module '{node.module}' which does not exist.")]
        for a in node.names:
            if _read(project, files, f"{target}/{a.name}.py") is None:
                out.append(_issue(rel, f"'{a.name}' is not a module in package '{node.module}'."))
        return out
    try:
        defined = _top_names(ast.parse(src))
    except SyntaxError:
        return []
    for a in node.names:
        if a.name != "*" and a.name not in defined:
            out.append(_issue(rel, f"Imports '{a.name}' from '{node.module}' but it is not defined there.",
                              f"Define '{a.name}' in {target}.py or import the correct name."))
    return out


def _check_js_imports(project, files, rel, text) -> list[dict]:
    out = []
    for m in re.finditer(r'import\s*\{([^}]*)\}\s*from\s*["\'](\.{1,2}/[^"\']+)["\']', text):
        names = [n.strip().split(" as ")[0].strip() for n in m.group(1).split(",") if n.strip()]
        target = os.path.normpath(str(Path(rel).parent / m.group(2))).replace("\\", "/")
        src = _read(project, files, target)
        if src is None:
            out.append(_issue(rel, f"Imports from missing module '{m.group(2)}'."))
            continue
        for n in names:
            if not re.search(rf"export\s+(?:async\s+)?(?:function\*?|const|let|var|class)\s+{re.escape(n)}\b|export\s*\{{[^}}]*\b{re.escape(n)}\b", src):
                out.append(_issue(rel, f"Imports '{n}' from '{m.group(2)}' but that module does not export it."))
    return out


def ensure_venv(cache_root: Path, requirements: str) -> str:
    """A clean virtualenv built ONLY from the generated project's requirements.txt, so the smoke test sees
    exactly what the end user will have (the platform's own packages, e.g. pydantic-settings, are not visible).
    Cached per requirements hash. Falls back to the platform interpreter if it cannot be created."""
    key = hashlib.sha1(requirements.encode()).hexdigest()[:10] + f"-py{sys.version_info.major}{sys.version_info.minor}"
    cache_root = cache_root.resolve()   # OUTPUT_DIR is relative; subprocesses run with another cwd
    venv = cache_root / key
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if py.exists() and (venv / ".ok").exists():
        return str(py)
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True, timeout=300)
        req = venv / "requirements.txt"
        req.write_text(requirements, encoding="utf-8")
        subprocess.run([str(py), "-m", "pip", "install", "-q", "-r", str(req)], check=True, capture_output=True, timeout=900)
        (venv / ".ok").write_text("ok")
        return str(py)
    except Exception:  # noqa: BLE001
        return sys.executable


def _example_env(project: Path) -> dict:
    """Values from the generated .env.example, so the format users will really copy is what gets tested."""
    out = {}
    p = project / ".env.example"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def _smoke_env(project: Path) -> dict:
    tmp = tempfile.mkdtemp()
    env = {**os.environ, **_example_env(project)}
    env.update({"DATABASE_URL": f"sqlite:///{tmp}/smoke.db", "SECRET_KEY": "smoke-test-secret-key-0123456789abcdef",
                "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "smoke-test-pass-123", "LLM_BASE_URL": "",
                "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def import_audit(project: Path, files: dict, python: str) -> list[dict]:
    """Really import every third-party module and name the project uses, in the clean environment."""
    targets = []
    for rel, text in files.items():
        if not rel.endswith(".py"):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods = [(a.name, []) for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                mods = [(n.module, [a.name for a in n.names])]
            else:
                continue
            for mod, names in mods:
                top = mod.split(".")[0]
                if top not in STDLIB and top != "backend":
                    targets.append([rel, mod, names])
    if not targets:
        return []
    tf = Path(tempfile.mkdtemp()) / "targets.json"
    tf.write_text(json.dumps(targets))
    try:
        p = subprocess.run([python, "-c", AUDIT_CODE, str(tf)], cwd=project, env=_smoke_env(project),
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return []
    m = re.search(r"AUDIT=(\[.*\])", p.stdout)
    if not m:
        return [_issue("backend/main.py", "Import audit crashed:\n" + (p.stdout + p.stderr)[-1200:])]
    seen, out = set(), []
    for f, problem in json.loads(m.group(1)):
        if (f, problem) not in seen:
            seen.add((f, problem))
            out.append(_issue(f, problem, "Use only names that exist in the installed libraries from requirements.txt."))
    return out


def _route_owner(files: dict, route: str) -> str:
    """Best guess of the router file that declares `route` (longest matching decorator path)."""
    best, blen = "backend/main.py", -1
    for rel, text in files.items():
        if rel.startswith("backend/routers/"):
            for lit in re.findall(r"@\w+\.(?:get|post|put|delete|patch)\(\s*[\"']([^\"']*)[\"']", text):
                if lit and route.endswith(lit) and len(lit) > blen:
                    best, blen = rel, len(lit)
    return best


def smoke_test(project: Path, files: dict, python: str | None = None) -> tuple[list[dict], list]:
    """Boot the generated app in-process and hit it. Returns (issues, routes[(METHOD, path)])."""
    python = python or sys.executable
    try:
        p = subprocess.run([python, "-c", SMOKE_CODE], cwd=project, env=_smoke_env(project),
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return [_issue("backend/main.py", "App did not start within 120 seconds.")], []
    out = p.stdout + p.stderr
    routes = []
    m = re.search(r"ROUTES=(\[.*\])", out)
    if m:
        routes = [tuple(r) for r in json.loads(m.group(1))]
    if "SMOKE_OK" not in p.stdout:
        return [_issue("backend/main.py", "The app failed to boot or serve:\n" + out[-2500:])], routes
    issues = js_api_mismatches(files, [pth for _, pth in routes])
    m = re.search(r"BAD=(\[.*\])", p.stdout)
    for pth, why in (json.loads(m.group(1)) if m else []):
        hits = [h for h in re.findall(r"(backend/[\w/]+\.py)", why) if h in files]
        owner = hits[-1] if hits else _route_owner(files, pth)
        issues.append(_issue(owner, f"GET {pth} fails without credentials: {why[:400]}",
                             "A public or protected GET must answer 2xx/4xx, never a server error."))
    return issues, routes


def js_api_mismatches(files: dict, paths: list[str]) -> list[dict]:
    if not paths:
        return []
    pats = [re.compile("^" + re.sub(r"\\\{.*?\\\}", "[^/]+", re.escape(p)) + "/?$") for p in paths]
    issues = []
    for rel, text in files.items():
        if rel.startswith("frontend/") and rel.endswith((".js", ".html")):
            for lit in set(re.findall(r"[\"'`](/api/[^\"'`\s?]*)", text)):
                probe = re.sub(r"\$\{[^}]*\}", "1", lit)
                if not any(p.match(probe) for p in pats):
                    issues.append(_issue(rel, f"Calls '{lit}' but the backend has no matching route.",
                                         "Use an endpoint from the API contract."))
    return issues


def run_automated(project: Path, files: dict) -> tuple[list[dict], list]:
    issues = static_checks(project, files)
    routes = []
    if not any("Syntax error" in i["problem"] or "is missing" in i["problem"] for i in issues):
        req_file = project / "requirements.txt"
        reqs = req_file.read_text(encoding="utf-8") if req_file.exists() else ""
        python = ensure_venv(project.parent / ".smoke_venv", reqs) if reqs else sys.executable
        audit = import_audit(project, files, python)
        issues += audit
        if not audit:  # the audit already explains a failed boot; avoid duplicate noise
            smoke_issues, routes = smoke_test(project, files, python)
            issues += smoke_issues
    return issues, routes


def assign_file(issue: dict, files: dict) -> dict:
    """Make sure each issue points at a real project file (smoke tracebacks name files in the text)."""
    if issue["file"] in files:
        return issue
    hit = re.findall(r"(backend/[\w/]+\.py|frontend/[\w/.-]+)", issue["problem"])
    issue["file"] = next((h for h in hit[::-1] if h in files), "backend/main.py")
    return issue




# """Objective, non-LLM checks. Issues are dicts: {file, severity, problem, fix, source}."""
# import ast
# import json
# import os
# import re
# import subprocess
# import sys
# import tempfile
# from pathlib import Path

# ALLOWED_THIRD_PARTY = {
#     "fastapi", "starlette", "uvicorn", "sqlalchemy", "pydantic", "dotenv", "httpx",
#     "jwt", "bcrypt", "multipart", "backend", "typing_extensions", "anyio",
# }
# STDLIB = set(getattr(sys, "stdlib_module_names", set())) | {"__future__"}

# SMOKE_CODE = """
# import json
# from fastapi.testclient import TestClient
# from backend.main import app
# with TestClient(app) as c:
#     r = c.get('/')
#     assert r.status_code == 200, 'GET / returned %s' % r.status_code
#     r = c.get('/openapi.json')
#     assert r.status_code == 200, 'GET /openapi.json returned %s' % r.status_code
#     routes = [[m.upper(), p] for p, ops in r.json().get('paths', {}).items() for m in ops]
#     print('ROUTES=' + json.dumps(routes))
# print('SMOKE_OK')
# """


# def _issue(file, problem, fix=""):
#     return {"file": file, "severity": "blocker", "problem": problem, "fix": fix, "source": "auto"}


# def _read(project: Path, files: dict, rel: str):
#     if rel in files:
#         return files[rel]
#     p = project / rel
#     return p.read_text(encoding="utf-8") if p.exists() else None


# def _top_names(tree) -> set:
#     names = set()
#     for n in tree.body:
#         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
#             names.add(n.name)
#         elif isinstance(n, ast.Assign):
#             names |= {t.id for t in n.targets if isinstance(t, ast.Name)}
#         elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
#             names.add(n.target.id)
#         elif isinstance(n, (ast.Import, ast.ImportFrom)):
#             names |= {(a.asname or a.name).split(".")[0] for a in n.names}
#     return names


# def static_checks(project: Path, files: dict) -> list[dict]:
#     issues = []
#     for req in ("backend/main.py", "frontend/index.html", "frontend/css/styles.css"):
#         if req not in files:
#             issues.append(_issue(req, f"Required file {req} is missing."))

#     for rel, text in files.items():
#         if rel.endswith(".py"):
#             try:
#                 tree = ast.parse(text)
#             except SyntaxError as e:
#                 issues.append(_issue(rel, f"Syntax error at line {e.lineno}: {e.msg}"))
#                 continue
#             for node in ast.walk(tree):
#                 if isinstance(node, ast.Import):
#                     mods = [a.name.split(".")[0] for a in node.names]
#                 elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
#                     mods = [node.module.split(".")[0]]
#                 else:
#                     continue
#                 for m in mods:
#                     if m not in STDLIB and m not in ALLOWED_THIRD_PARTY:
#                         issues.append(_issue(rel, f"Imports disallowed library '{m}'.", "Use only the allowed libraries."))
#                 if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and mods[0] == "backend":
#                     issues += _check_backend_import(project, files, rel, node)

#     main = files.get("backend/main.py", "")
#     if main and not re.search(r"^\s*app\s*=\s*FastAPI", main, re.M):
#         issues.append(_issue("backend/main.py", "Does not define `app = FastAPI(...)`."))

#     for rel, text in files.items():
#         if rel.endswith(".html"):
#             for ref in re.findall(r'(?:src|href)="([^"]+)"', text):
#                 if re.match(r"(https?:|//|data:|mailto:|tel:|javascript:|#|/api/)", ref):
#                     continue
#                 ref = ref.split("#")[0].split("?")[0]
#                 if not ref:
#                     continue
#                 target = Path("frontend") / ref.lstrip("/") if ref.startswith("/") else Path(rel).parent / ref
#                 norm = os.path.normpath(str(target)).replace("\\", "/")
#                 if _read(project, files, norm) is None and not (project / norm).exists():
#                     issues.append(_issue(rel, f"References missing file '{ref}'."))
#         if rel.endswith(".js"):
#             issues += _check_js_imports(project, files, rel, text)
#     return issues


# def _check_backend_import(project, files, rel, node) -> list[dict]:
#     out = []
#     target = node.module.replace(".", "/")
#     src = _read(project, files, target + ".py")
#     if src is None:
#         pkg = _read(project, files, target + "/__init__.py")
#         if pkg is None:
#             return [_issue(rel, f"Imports module '{node.module}' which does not exist.")]
#         for a in node.names:
#             if _read(project, files, f"{target}/{a.name}.py") is None:
#                 out.append(_issue(rel, f"'{a.name}' is not a module in package '{node.module}'."))
#         return out
#     try:
#         defined = _top_names(ast.parse(src))
#     except SyntaxError:
#         return []
#     for a in node.names:
#         if a.name != "*" and a.name not in defined:
#             out.append(_issue(rel, f"Imports '{a.name}' from '{node.module}' but it is not defined there.",
#                               f"Define '{a.name}' in {target}.py or import the correct name."))
#     return out


# def _check_js_imports(project, files, rel, text) -> list[dict]:
#     out = []
#     for m in re.finditer(r'import\s*\{([^}]*)\}\s*from\s*["\'](\.{1,2}/[^"\']+)["\']', text):
#         names = [n.strip().split(" as ")[0].strip() for n in m.group(1).split(",") if n.strip()]
#         target = os.path.normpath(str(Path(rel).parent / m.group(2))).replace("\\", "/")
#         src = _read(project, files, target)
#         if src is None:
#             out.append(_issue(rel, f"Imports from missing module '{m.group(2)}'."))
#             continue
#         for n in names:
#             if not re.search(rf"export\s+(?:async\s+)?(?:function\*?|const|let|var|class)\s+{re.escape(n)}\b|export\s*\{{[^}}]*\b{re.escape(n)}\b", src):
#                 out.append(_issue(rel, f"Imports '{n}' from '{m.group(2)}' but that module does not export it."))
#     return out


# def smoke_test(project: Path, files: dict) -> tuple[list[dict], list]:
#     """Boot the generated app in-process and hit it. Returns (issues, routes[(METHOD, path)])."""
#     tmp = tempfile.mkdtemp()
#     env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp}/smoke.db", "SECRET_KEY": "smoke-test-secret-key-0123456789abcdef",
#            "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "smoke-test-pass-123", "LLM_BASE_URL": "", "PYTHONDONTWRITEBYTECODE": "1"}
#     try:
#         p = subprocess.run([sys.executable, "-c", SMOKE_CODE], cwd=project, env=env, capture_output=True, text=True, timeout=120)
#     except subprocess.TimeoutExpired:
#         return [_issue("backend/main.py", "App did not start within 120 seconds.")], []
#     out = p.stdout + p.stderr
#     routes = []
#     m = re.search(r"ROUTES=(\[.*\])", out)
#     if m:
#         routes = [tuple(r) for r in json.loads(m.group(1))]
#     if "SMOKE_OK" not in p.stdout:
#         return [_issue("backend/main.py", "The app failed to boot or serve:\n" + out[-2500:])], routes
#     return js_api_mismatches(files, [p for _, p in routes]), routes


# def js_api_mismatches(files: dict, paths: list[str]) -> list[dict]:
#     if not paths:
#         return []
#     pats = [re.compile("^" + re.sub(r"\\\{.*?\\\}", "[^/]+", re.escape(p)) + "/?$") for p in paths]
#     issues = []
#     for rel, text in files.items():
#         if rel.startswith("frontend/") and rel.endswith((".js", ".html")):
#             for lit in set(re.findall(r"[\"'`](/api/[^\"'`\s?]*)", text)):
#                 probe = re.sub(r"\$\{[^}]*\}", "1", lit)
#                 if not any(p.match(probe) for p in pats):
#                     issues.append(_issue(rel, f"Calls '{lit}' but the backend has no matching route.",
#                                          "Use an endpoint from the API contract."))
#     return issues


# def run_automated(project: Path, files: dict) -> tuple[list[dict], list]:
#     issues = static_checks(project, files)
#     routes = []
#     if not any("Syntax error" in i["problem"] or "is missing" in i["problem"] for i in issues):
#         smoke_issues, routes = smoke_test(project, files)
#         issues += smoke_issues
#     return issues, routes


# def assign_file(issue: dict, files: dict) -> dict:
#     """Make sure each issue points at a real project file (smoke tracebacks name files in the text)."""
#     if issue["file"] in files:
#         return issue
#     hit = re.findall(r"(backend/[\w/]+\.py|frontend/[\w/.-]+)", issue["problem"])
#     issue["file"] = next((h for h in hit[::-1] if h in files), "backend/main.py")
#     return issue
