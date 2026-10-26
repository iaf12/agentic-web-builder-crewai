"""The architect's work: spec, API contract, file manifest, QA checklist."""
from studio.prompts import SECURITY_BASELINE, STACK_RULES
from studio.util import extract_json

ALLOWED_OWNERS = {"designer", "backend", "frontend"}
REQUIRED = {
    "backend/config.py": ("backend", "Settings read from environment variables"),
    "backend/database.py": ("backend", "SQLAlchemy engine, session and Base"),
    "backend/models.py": ("backend", "ORM models"),
    "backend/schemas.py": ("backend", "Pydantic request/response schemas"),
    "backend/security.py": ("backend", "Password hashing, JWT auth dependency, rate limiter"),
    "backend/main.py": ("backend", "FastAPI app, lifespan, routers, static frontend"),
    "frontend/css/styles.css": ("designer", "Complete design system and component styles"),
    "frontend/js/common.js": ("frontend", "Shared helpers: api(), el(), toast()"),
    "frontend/index.html": ("frontend", "Home page"),
}


def make_spec(eng, requirement: str, answers: str) -> str:
    task = f"""CLIENT REQUIREMENT:
{requirement}

CLIENT ANSWERS TO YOUR QUESTIONS:
{answers}

{STACK_RULES}
{SECURITY_BASELINE}
Write the PRODUCT SPEC in Markdown (max about 1500 words) with exactly these sections:
## 1. Overview and goals
## 2. Architect additions  (at least 5 important things the client did not ask for but the project needs
   to be excellent and secure; e.g. admin dashboard, conversation logging, rate limiting, privacy notice,
   empty/error states, SEO basics, health endpoint)
## 3. Brand and design direction  (product name, tone, palette idea, hero headline and subtext)
## 4. Pages  (for each page: file name, purpose, sections, the exact DOM element ids its script will use,
   and which API endpoints it calls)
## 5. Data model  (tables, columns with types, relations)
Be concrete. Do not write code."""
    return eng.ask("architect", [(task, 0)]).strip()


def make_api(eng, spec: str) -> str:
    task = f"""{STACK_RULES}
{SECURITY_BASELINE}
Using the spec below, write the API CONTRACT and SECURITY PLAN in Markdown (max about 1300 words):
## 6. API contract
For EVERY endpoint, one block: `METHOD /api/path` | auth: public or admin | request JSON | response JSON
| error cases. Include a GET /api/health endpoint. Use consistent field names with the data model.
## 7. Security plan
Project-specific measures beyond the baseline.
Do not write code."""
    return eng.ask("architect", [(task, 0), ("SPEC:\n" + spec, 3)]).strip()


def make_manifest(eng, spec: str, api: str, log) -> list[dict]:
    task = """Produce the FILE MANIFEST for this project as ONLY a JSON array (no prose, no fences).
Each item: {"path": "...", "owner": "designer|backend|frontend", "purpose": "one line", "depends_on": ["paths written before it that it needs"]}.
Rules:
- Paths must start with backend/ or frontend/. Do NOT list __init__.py files.
- Must include: backend/config.py, database.py, models.py, schemas.py, security.py, main.py, one
  backend/routers/<name>.py per API area, frontend/css/styles.css (owner designer),
  frontend/js/common.js, frontend/index.html, and for every other page in the spec one
  frontend/<page>.html plus frontend/js/<page>.js.
- depends_on: each router depends on models, schemas, security, database; main.py depends on all routers;
  each page .html depends on styles.css; each page .js depends on common.js and its .html.
- At most 22 files. Keep the project focused."""
    for attempt in range(3):
        out = eng.ask("architect", [(task, 0), ("SPEC:\n" + spec, 3), ("API CONTRACT:\n" + api, 2)])
        try:
            return normalize_manifest(extract_json(out))
        except Exception as e:  # noqa: BLE001
            log(f"Manifest was invalid ({e}); asking the architect again ({attempt + 1}/3)")
    raise RuntimeError("The architect could not produce a valid file manifest.")


def normalize_manifest(items) -> list[dict]:
    seen: dict[str, dict] = {}
    for it in items:
        path = str(it.get("path", "")).strip().replace("\\", "/")
        owner = str(it.get("owner", "")).strip().lower()
        if not path.startswith(("backend/", "frontend/")) or ".." in path or path.endswith("__init__.py"):
            continue
        if owner not in ALLOWED_OWNERS:
            owner = "backend" if path.startswith("backend/") else ("designer" if path.endswith(".css") else "frontend")
        seen.setdefault(path, {"path": path, "owner": owner, "purpose": str(it.get("purpose", ""))[:140],
                               "depends_on": [str(d) for d in it.get("depends_on", []) if isinstance(d, str)]})
    for path, (owner, purpose) in REQUIRED.items():
        seen.setdefault(path, {"path": path, "owner": owner, "purpose": purpose, "depends_on": []})
    seen["frontend/css/styles.css"]["owner"] = "designer"
    manifest = list(seen.values())[:24]
    paths = {m["path"] for m in manifest}
    for m in manifest:
        m["depends_on"] = [d for d in dict.fromkeys(m["depends_on"]) if d in paths and d != m["path"]]
    return manifest


def make_checklist(eng, spec: str, api: str) -> str:
    task = """Write the QA ACCEPTANCE CHECKLIST for this project: 18 to 25 concrete, verifiable items, one per line,
formatted `- [CHK-n] (files: a, b) statement that is clearly pass or fail`. Cover: every endpoint exists with the
exact method/path/JSON shape; auth on admin routes; validation limits; rate limiting; admin seeding; each page
loads and uses only existing ids/classes/endpoints; loading/empty/error states; XSS-safe rendering;
privacy notice; responsive and dark theme; no hardcoded secrets; app boots with `python run.py`."""
    return eng.ask("architect", [(task, 0), ("SPEC:\n" + spec, 3), ("API CONTRACT:\n" + api, 2)]).strip()
