
## Fixed update
"""System prompts, tech stack constraints, and output formats."""
 
STACK_RULES = """FIXED TECH STACK (non-negotiable, so the finished project runs with
`pip install -r requirements.txt` then `python run.py`):
 
- Backend: Python 3.10+, FastAPI, SQLAlchemy 2.x (SQLite), Pydantic v2, python-dotenv, httpx,
  PyJWT, bcrypt, python-multipart. NO other third-party libraries. Standard library is fine.
- Frontend: plain HTML + CSS + vanilla JavaScript ES modules. No npm, no build step, no frameworks,
  no CDN required for core function. Served by FastAPI StaticFiles.
- Required layout (the platform already provides run.py, requirements.txt, .env.example, README.md,
  backend/__init__.py and backend/routers/__init__.py; do not write those):
    backend/main.py      -> defines `app = FastAPI(..., lifespan=lifespan)`. The lifespan creates DB tables
                            and seeds the admin user. Includes every router with prefix "/api".
                            Mounts the frontend at "/" LAST:
                            app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
                            with FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
    backend/config.py    -> settings from env vars with safe defaults (module-level constants)
    backend/database.py  -> engine, SessionLocal, Base (DeclarativeBase), get_db dependency.
                            Default DATABASE_URL sqlite:///./data/app.db ; create data/ if missing;
                            connect_args={"check_same_thread": False} for SQLite.
    backend/models.py, backend/schemas.py, backend/security.py (password hashing, JWT, auth dependency,
                            in-memory rate limiter)
    backend/routers/<name>.py -> each defines `router = APIRouter(...)`
    frontend/index.html, frontend/<page>.html, frontend/css/styles.css, frontend/js/common.js,
    frontend/js/<page>.js
- All backend imports are absolute and start with `backend.` (e.g. `from backend.database import get_db`).
- Every API route lives under /api/... . Frontend code calls them with absolute paths,
  e.g. fetch("/api/chat"), never http://localhost...
- Env vars: DATABASE_URL, SECRET_KEY, ADMIN_USERNAME, ADMIN_PASSWORD, CORS_ORIGINS,
  LLM_BASE_URL, LLM_API_KEY, LLM_MODEL. Never hardcode secrets.
- AI/chatbot features call an OpenAI-compatible endpoint via httpx:
  POST {LLM_BASE_URL}/chat/completions (with timeout and error handling). If LLM_BASE_URL is empty,
  return a friendly fallback message instead of crashing.
 
HARD RULES (each of these broke a previous build; the platform rejects code that violates them):
1. SYNCHRONOUS database code only: create_engine, sessionmaker, Session, and plain `def` routes/dependencies.
   NEVER use create_async_engine, AsyncSession, aiosqlite, sqlalchemy.ext.asyncio or a "sqlite+aiosqlite" URL.
   DATABASE_URL is always a plain "sqlite:///..." URL.
2. backend/config.py is PLAIN PYTHON. NEVER use pydantic BaseSettings or the pydantic-settings package.
   Write: `from dotenv import load_dotenv; load_dotenv()` then module-level constants from os.getenv, e.g.
     SECRET_KEY, ALGORITHM = "HS256", ACCESS_TOKEN_EXPIRE_MINUTES, DATABASE_URL, ADMIN_USERNAME, ADMIN_PASSWORD,
     CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]   # comma-separated
     LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
   Every other file reads them with `from backend.config import NAME`. Never invent a constant that config.py lacks.
3. IMPORT DIRECTION. A module may import only modules to its LEFT, never to its right, so there are no cycles:
     config  <  database  <  models  <  schemas  <  security  <  routers/*  <  main
   Example: database.py imports only config. Seeding the admin user and creating tables happen inside the
   lifespan in main.py, never in database.py or models.py.
4. Import only names that really exist. Typical correct imports:
     from sqlalchemy import create_engine, select, String, ForeignKey
     from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, Session, sessionmaker
   Do not import Dialect or other engine internals. Use only the libraries listed above.
5. Every GET route must work without crashing when called with no token and no parameters (return 200, 401 or 422,
   never a server error).
"""
 
SECURITY_BASELINE = """SECURITY BASELINE (every project must satisfy this):
- Admin area protected by login: bcrypt password hash, JWT (PyJWT) with expiry, Bearer token.
- Admin user seeded from ADMIN_USERNAME / ADMIN_PASSWORD on startup.
- Validate all input with Pydantic (types, min/max lengths). SQLAlchemy ORM only (no string-built SQL).
- Simple in-memory rate limiting on public write/chat endpoints (in backend/security.py).
- CORS restricted by CORS_ORIGINS. Never return stack traces to clients.
- Frontend renders user/AI text with textContent, never innerHTML with untrusted data.
- If conversations or personal data are stored, show a short privacy notice in the UI.
"""
 
ROLE_RULES = {
    "designer": """DESIGN QUALITY BAR (make it look like a real, funded product, never a wireframe):
- CSS custom properties for colours/spacing/radius/shadows, fluid type with clamp(), CSS grid/flex,
  modern features (color-mix, :focus-visible, prefers-color-scheme dark theme, prefers-reduced-motion).
- Brand-fitting palette with one accent gradient, soft shadows, rounded cards, 8px spacing scale,
  150-200ms transitions, hover/active states, sticky blurred header, responsive nav with mobile menu.
- Ready-made classes for: hero, section, card grid, buttons (primary/secondary/ghost), forms and inputs,
  chat window with message bubbles and typing indicator, tables, badges, alerts, toast, modal (dialog),
  skeleton loader, empty state, admin layout (sidebar + content), footer.
- Complete file, well organised with section comments. Under about 450 lines.""",
 
    "backend": """MODERN BACKEND PRACTICES:
- FastAPI lifespan (not on_event), dependency injection with Depends, correct status codes, HTTPException.
- Pydantic v2: ConfigDict(from_attributes=True), Field(min_length/max_length) constraints.
- SQLAlchemy 2.0 typed ORM: DeclarativeBase, Mapped[...] + mapped_column, select(), session.scalars().
- Type hints everywhere, timezone-aware datetimes (datetime.now(timezone.utc)), small focused functions.
- Protected routes use the auth dependency. Public write/chat routes use the rate limiter.
- Use exactly the route paths, methods and JSON shapes in the API contract.""",
 
    "frontend": """MODERN FRONTEND PRACTICES:
- ES modules: pages load `<script type="module" src="js/<page>.js">`; share helpers by
  `import { api, el, toast } from "./common.js"` (only import names that file really exports).
- async/await with fetch and AbortController timeouts, one `api()` helper in common.js that adds the
  Bearer token from sessionStorage and throws readable errors.
- Semantic HTML5, labels for every input, ARIA where needed, keyboard accessible.
- Use ONLY CSS classes that exist in frontend/css/styles.css and ONLY element ids the blueprint defines.
- Handle loading, empty and error states. Render untrusted text with textContent.
- Real, domain-appropriate copy (no lorem ipsum). Use inline SVG for icons.""",
}
 
FILE_FORMAT = """
OUTPUT FORMAT. Output exactly one file, complete, with this wrapper and nothing else
(no markdown fences inside it, no placeholders, no "...", no TODOs, no commentary):
 
=== FILE: {path} ===
<entire file content>
=== END FILE ===
"""
 
PATCH_FORMAT = """
OUTPUT FORMAT FOR FIXES:
Make precise, surgical changes. Use one or more SEARCH/REPLACE blocks formatted exactly like this:
 
<<<<<<< SEARCH
exact lines to find and replace
=======
replacement lines
>>>>>>> REPLACE
 
Rules for SEARCH/REPLACE:
1. The SEARCH block must match existing lines in the file character-for-character, including whitespace.
2. Include 2-3 lines of unchanged surrounding code context in both SEARCH and REPLACE blocks.
3. If the entire file needs to be rewritten from scratch, use the full file format instead:
=== FILE: {path} ===
<entire file content>
=== END FILE ===
"""






# """System prompts, tech stack constraints, and output formats."""

# STACK_RULES = """FIXED TECH STACK (non-negotiable, so the finished project runs with
# `pip install -r requirements.txt` then `python run.py`):

# - Backend: Python 3.10+, FastAPI, SQLAlchemy 2.x (SQLite), Pydantic v2, python-dotenv, httpx,
#   PyJWT, bcrypt, python-multipart. NO other third-party libraries. Standard library is fine.
# - Frontend: plain HTML + CSS + vanilla JavaScript ES modules. No npm, no build step, no frameworks,
#   no CDN required for core function. Served by FastAPI StaticFiles.
# - Required layout (the platform already provides run.py, requirements.txt, .env.example, README.md,
#   backend/__init__.py and backend/routers/__init__.py; do not write those):
#     backend/main.py      -> defines `app = FastAPI(..., lifespan=lifespan)`. The lifespan creates DB tables
#                             and seeds the admin user. Includes every router with prefix "/api".
#                             Mounts the frontend at "/" LAST:
#                             app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
#                             with FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
#     backend/config.py    -> settings from env vars with safe defaults (module-level constants)
#     backend/database.py  -> engine, SessionLocal, Base (DeclarativeBase), get_db dependency.
#                             Default DATABASE_URL sqlite:///./data/app.db ; create data/ if missing;
#                             connect_args={"check_same_thread": False} for SQLite.
#     backend/models.py, backend/schemas.py, backend/security.py (password hashing, JWT, auth dependency,
#                             in-memory rate limiter)
#     backend/routers/<name>.py -> each defines `router = APIRouter(...)`
#     frontend/index.html, frontend/<page>.html, frontend/css/styles.css, frontend/js/common.js,
#     frontend/js/<page>.js
# - All backend imports are absolute and start with `backend.` (e.g. `from backend.database import get_db`).
# - Every API route lives under /api/... . Frontend code calls them with absolute paths,
#   e.g. fetch("/api/chat"), never http://localhost...
# - Env vars: DATABASE_URL, SECRET_KEY, ADMIN_USERNAME, ADMIN_PASSWORD, CORS_ORIGINS,
#   LLM_BASE_URL, LLM_API_KEY, LLM_MODEL. Never hardcode secrets.
# - AI/chatbot features call an OpenAI-compatible endpoint via httpx:
#   POST {LLM_BASE_URL}/chat/completions (with timeout and error handling). If LLM_BASE_URL is empty,
#   return a friendly fallback message instead of crashing.
# """

# SECURITY_BASELINE = """SECURITY BASELINE (every project must satisfy this):
# - Admin area protected by login: bcrypt password hash, JWT (PyJWT) with expiry, Bearer token.
# - Admin user seeded from ADMIN_USERNAME / ADMIN_PASSWORD on startup.
# - Validate all input with Pydantic (types, min/max lengths). SQLAlchemy ORM only (no string-built SQL).
# - Simple in-memory rate limiting on public write/chat endpoints (in backend/security.py).
# - CORS restricted by CORS_ORIGINS. Never return stack traces to clients.
# - Frontend renders user/AI text with textContent, never innerHTML with untrusted data.
# - If conversations or personal data are stored, show a short privacy notice in the UI.
# """

# ROLE_RULES = {
#     "designer": """DESIGN QUALITY BAR (make it look like a real, funded product, never a wireframe):
# - CSS custom properties for colours/spacing/radius/shadows, fluid type with clamp(), CSS grid/flex,
#   modern features (color-mix, :focus-visible, prefers-color-scheme dark theme, prefers-reduced-motion).
# - Brand-fitting palette with one accent gradient, soft shadows, rounded cards, 8px spacing scale,
#   150-200ms transitions, hover/active states, sticky blurred header, responsive nav with mobile menu.
# - Ready-made classes for: hero, section, card grid, buttons (primary/secondary/ghost), forms and inputs,
#   chat window with message bubbles and typing indicator, tables, badges, alerts, toast, modal (dialog),
#   skeleton loader, empty state, admin layout (sidebar + content), footer.
# - Complete file, well organised with section comments. Under about 450 lines.""",

#     "backend": """MODERN BACKEND PRACTICES:
# - FastAPI lifespan (not on_event), dependency injection with Depends, correct status codes, HTTPException.
# - Pydantic v2: ConfigDict(from_attributes=True), Field(min_length/max_length) constraints.
# - SQLAlchemy 2.0 typed ORM: DeclarativeBase, Mapped[...] + mapped_column, select(), session.scalars().
# - Type hints everywhere, timezone-aware datetimes (datetime.now(timezone.utc)), small focused functions.
# - Protected routes use the auth dependency. Public write/chat routes use the rate limiter.
# - Use exactly the route paths, methods and JSON shapes in the API contract.""",

#     "frontend": """MODERN FRONTEND PRACTICES:
# - ES modules: pages load `<script type="module" src="js/<page>.js">`; share helpers by
#   `import { api, el, toast } from "./common.js"` (only import names that file really exports).
# - async/await with fetch and AbortController timeouts, one `api()` helper in common.js that adds the
#   Bearer token from sessionStorage and throws readable errors.
# - Semantic HTML5, labels for every input, ARIA where needed, keyboard accessible.
# - Use ONLY CSS classes that exist in frontend/css/styles.css and ONLY element ids the blueprint defines.
# - Handle loading, empty and error states. Render untrusted text with textContent.
# - Real, domain-appropriate copy (no lorem ipsum). Use inline SVG for icons.""",
# }

# FILE_FORMAT = """
# OUTPUT FORMAT. Output exactly one file, complete, with this wrapper and nothing else
# (no markdown fences inside it, no placeholders, no "...", no TODOs, no commentary):

# === FILE: {path} ===
# <entire file content>
# === END FILE ===
# """

# PATCH_FORMAT = """
# OUTPUT FORMAT FOR FIXES:
# Make precise, surgical changes. Use one or more SEARCH/REPLACE blocks formatted exactly like this:

# <<<<<<< SEARCH
# exact lines to find and replace
# =======
# replacement lines
# >>>>>>> REPLACE

# Rules for SEARCH/REPLACE:
# 1. The SEARCH block must match existing lines in the file character-for-character, including whitespace.
# 2. Include 2-3 lines of unchanged surrounding code context in both SEARCH and REPLACE blocks.
# 3. If the entire file needs to be rewritten from scratch, use the full file format instead:
# === FILE: {path} ===
# <entire file content>
# === END FILE ===
# """

















## Claude 

# STACK_RULES = """
# FIXED TECH STACK (non-negotiable, so the finished project runs with
# `pip install -r requirements.txt` then `python run.py`):

# - Backend: Python 3.10+, FastAPI, SQLAlchemy 2.x (SQLite), Pydantic v2, python-dotenv, httpx,
#   PyJWT, bcrypt, python-multipart. NO other third-party libraries. Standard library is fine.
# - Frontend: plain HTML + CSS + vanilla JavaScript ES modules. No npm, no build step, no frameworks,
#   no CDN required for core function. Served by FastAPI StaticFiles.
# - Required layout (the platform already provides run.py, requirements.txt, .env.example, README.md,
#   backend/__init__.py and backend/routers/__init__.py; do not write those):
#     backend/main.py      -> defines `app = FastAPI(..., lifespan=lifespan)`. The lifespan creates DB tables
#                             and seeds the admin user. Includes every router with prefix "/api".
#                             Mounts the frontend at "/" LAST:
#                             app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
#                             with FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
#     backend/config.py    -> settings from env vars with safe defaults (module-level constants)
#     backend/database.py  -> engine, SessionLocal, Base (DeclarativeBase), get_db dependency.
#                             Default DATABASE_URL sqlite:///./data/app.db ; create data/ if missing;
#                             connect_args={"check_same_thread": False} for SQLite.
#     backend/models.py, backend/schemas.py, backend/security.py (password hashing, JWT, auth dependency,
#                             in-memory rate limiter)
#     backend/routers/<name>.py -> each defines `router = APIRouter(...)`
#     frontend/index.html, frontend/<page>.html, frontend/css/styles.css, frontend/js/common.js,
#     frontend/js/<page>.js
# - All backend imports are absolute and start with `backend.` (e.g. `from backend.database import get_db`).
# - Every API route lives under /api/... . Frontend code calls them with absolute paths,
#   e.g. fetch("/api/chat"), never http://localhost...
# - Env vars: DATABASE_URL, SECRET_KEY, ADMIN_USERNAME, ADMIN_PASSWORD, CORS_ORIGINS,
#   LLM_BASE_URL, LLM_API_KEY, LLM_MODEL. Never hardcode secrets.
# - AI/chatbot features call an OpenAI-compatible endpoint via httpx:
#   POST {LLM_BASE_URL}/chat/completions (with timeout and error handling). If LLM_BASE_URL is empty,
#   return a friendly fallback message instead of crashing.
# """

# SECURITY_BASELINE = """
# SECURITY BASELINE (every project must satisfy this):
# - Admin area protected by login: bcrypt password hash, JWT (PyJWT) with expiry, Bearer token.
# - Admin user seeded from ADMIN_USERNAME / ADMIN_PASSWORD on startup.
# - Validate all input with Pydantic (types, min/max lengths). SQLAlchemy ORM only (no string-built SQL).
# - Simple in-memory rate limiting on public write/chat endpoints (in backend/security.py).
# - CORS restricted by CORS_ORIGINS. Never return stack traces to clients.
# - Frontend renders user/AI text with textContent, never innerHTML with untrusted data.
# - If conversations or personal data are stored, show a short privacy notice in the UI.
# """

# ROLE_RULES = {
#     "designer": """DESIGN QUALITY BAR (make it look like a real, funded product, never a wireframe):
# - CSS custom properties for colours/spacing/radius/shadows, fluid type with clamp(), CSS grid/flex,
#   modern features (color-mix, :focus-visible, prefers-color-scheme dark theme, prefers-reduced-motion).
# - Brand-fitting palette with one accent gradient, soft shadows, rounded cards, 8px spacing scale,
#   150-200ms transitions, hover/active states, sticky blurred header, responsive nav with mobile menu.
# - Ready-made classes for: hero, section, card grid, buttons (primary/secondary/ghost), forms and inputs,
#   chat window with message bubbles and typing indicator, tables, badges, alerts, toast, modal (dialog),
#   skeleton loader, empty state, admin layout (sidebar + content), footer.
# - Complete file, well organised with section comments. Under about 450 lines.""",
#     "backend": """MODERN BACKEND PRACTICES:
# - FastAPI lifespan (not on_event), dependency injection with Depends, correct status codes, HTTPException.
# - Pydantic v2: ConfigDict(from_attributes=True), Field(min_length/max_length) constraints.
# - SQLAlchemy 2.0 typed ORM: DeclarativeBase, Mapped[...] + mapped_column, select(), session.scalars().
# - Type hints everywhere, timezone-aware datetimes (datetime.now(timezone.utc)), small focused functions.
# - Protected routes use the auth dependency. Public write/chat routes use the rate limiter.
# - Use exactly the route paths, methods and JSON shapes in the API contract.""",
#     "frontend": """MODERN FRONTEND PRACTICES:
# - ES modules: pages load `<script type="module" src="js/<page>.js">`; share helpers by
#   `import { api, el, toast } from "./common.js"` (only import names that file really exports).
# - async/await with fetch and AbortController timeouts, one `api()` helper in common.js that adds the
#   Bearer token from sessionStorage and throws readable errors.
# - Semantic HTML5, labels for every input, ARIA where needed, keyboard accessible.
# - Use ONLY CSS classes that exist in frontend/css/styles.css and ONLY element ids the blueprint defines.
# - Handle loading, empty and error states. Render untrusted text with textContent.
# - Real, domain-appropriate copy (no lorem ipsum). Use inline SVG for icons.""",
# }

# FILE_FORMAT = """
# OUTPUT FORMAT. Output exactly one file, complete, with this wrapper and nothing else
# (no markdown fences inside it, no placeholders, no "...", no TODOs, no commentary):

# === FILE: {path} ===
# <entire file content>
# === END FILE ===
# """




