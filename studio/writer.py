import re
import zipfile
from pathlib import Path

FILE_RE = re.compile(r"=== FILE: (?P<path>[^\n]+?) ===\r?\n(?P<body>.*?)\r?\n=== END FILE ===", re.S)
ALLOWED_ROOTS = ("backend/", "frontend/")

PROJECT_REQUIREMENTS = """fastapi>=0.110.0
uvicorn[standard]>=0.29.0
sqlalchemy>=2.0.25
pydantic>=2.5.0
python-dotenv>=1.0.0
httpx>=0.27.0
PyJWT>=2.8.0
bcrypt>=4.1.0
python-multipart>=0.0.9
"""

ENV_EXAMPLE = """# Copy this file to .env and change the values.
SECRET_KEY=change-me-to-a-long-random-string
ADMIN_USERNAME=admin
ADMIN_PASSWORD=change-this-password
DATABASE_URL=sqlite:///./data/app.db
CORS_ORIGINS=http://127.0.0.1:8000,http://localhost:8000
HOST=127.0.0.1
PORT=8000
# Optional: OpenAI-compatible endpoint for the chatbot
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=
"""

RUN_PY = '''import os

import uvicorn
from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
    )
'''

def parse_files(text: str) -> dict[str, str]:
    files: dict[str, str] = {}
    for m in FILE_RE.finditer(text or ""):
        path = m.group("path").strip().strip("`").replace("\\", "/")
        body = m.group("body")
        if path.startswith("/") or ".." in path.split("/") or not path.startswith(ALLOWED_ROOTS):
            continue
        body = _strip_fence(body)
        files[path] = body.rstrip() + "\n"
    return files


def _strip_fence(body: str) -> str:
    lines = body.strip("\n").split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
    return "\n".join(lines)


def strip_files(text: str) -> str:
    """Return the prose of an agent answer with file blocks removed."""
    return FILE_RE.sub("", text or "").strip()


def zip_project(root: Path) -> Path:
    out = root.with_suffix(".zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(root.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and ".studio" not in p.parts and p.suffix != ".db":
                z.write(p, Path(root.name) / p.relative_to(root))
    return out


def write_scaffold(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in {"run.py": RUN_PY, "requirements.txt": PROJECT_REQUIREMENTS, ".env.example": ENV_EXAMPLE,
                         ".gitignore": ".env\n__pycache__/\ndata/\n.venv/\n",
                         "backend/__init__.py": "", "backend/routers/__init__.py": ""}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / ".gitkeep").write_text("", encoding="utf-8")


def write_file(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def tree_lines(entries: dict) -> list[str]:
    tree: dict = {}
    for path, desc in entries.items():
        node = tree
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part + "/", {})
        node[parts[-1]] = desc
    lines: list[str] = []

    def walk(node, prefix):
        items = sorted(node.items(), key=lambda kv: (not isinstance(kv[1], dict), kv[0]))
        for i, (name, val) in enumerate(items):
            last = i == len(items) - 1
            branch = "└── " if last else "├── "
            if isinstance(val, dict):
                lines.append(prefix + branch + name)
                walk(val, prefix + ("    " if last else "│   "))
            else:
                lines.append((prefix + branch + name).ljust(46) + "  # " + val)

    walk(tree, "")
    return lines


def render_readme(name: str, spec: str, manifest: list, routes: list, status: str) -> str:
    m = re.search(r"##\s*1\..*?\n(.*?)(?=\n##\s*2\.|\Z)", spec, re.S)
    overview = (m.group(1).strip() if m else "").strip()[:900]
    entries = {x["path"]: x["purpose"] for x in manifest}
    entries.update({"run.py": "Start the server (python run.py)", "requirements.txt": "Python dependencies",
                    ".env.example": "Copy to .env and edit", "docs/ARCHITECTURE.md": "Blueprint: spec, API contract, checklist",
                    "docs/QA_REPORT.md": "QA verdicts and sign-off", "data/": "SQLite database (created on first run)"})
    entries = {k: v for k, v in entries.items() if not k.endswith("/")}
    tree = "\n".join([name + "/"] + tree_lines(entries))
    pages = "\n".join(f"- `/{x['path'][len('frontend/'):]}`: {x['purpose']}" for x in manifest
                      if x["path"].startswith("frontend/") and x["path"].endswith(".html"))
    api_rows = "\n".join(f"| {mt} | `{p}` |" for mt, p in sorted(routes, key=lambda r: (r[1], r[0])) if p.startswith("/api"))
    return f"""# {name}

> Built by Crew Web Studio. QA status: **{status}**

{overview}

## Quick start
```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env             # set ADMIN_PASSWORD, SECRET_KEY and (optionally) the LLM endpoint
python run.py
```
Open http://127.0.0.1:8000 . Interactive API docs: http://127.0.0.1:8000/docs

## Project map
```
{tree}
```

## How a request flows
```
Browser page (frontend/*.html)
   └─ module script (frontend/js/<page>.js) ── api() in common.js ──▶ fetch("/api/...")
                                                                     │
FastAPI app (backend/main.py) ──▶ router (backend/routers/*.py) ──▶ security.py (auth, rate limit)
                                       │                                   
                                       └─▶ models.py / schemas.py ──▶ database.py ──▶ data/app.db (SQLite)
```

## Pages
{pages}

## API routes
| Method | Path |
|---|---|
{api_rows}

## Configuration (.env)
| Variable | Purpose |
|---|---|
| ADMIN_USERNAME / ADMIN_PASSWORD | Admin login seeded on first start |
| SECRET_KEY | Signs login tokens (use a long random string) |
| DATABASE_URL | Default `sqlite:///./data/app.db` |
| CORS_ORIGINS | Allowed browser origins |
| LLM_BASE_URL / LLM_API_KEY / LLM_MODEL | Optional OpenAI-compatible endpoint for chatbot features |
"""
