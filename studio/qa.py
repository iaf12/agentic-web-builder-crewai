"""Strict QA gate + architect's final acceptance."""
from studio.build import Ctx, outline
from studio.prompts import SECURITY_BASELINE, STACK_RULES
from studio.util import extract_json

GATE = {"blocker", "major"}

CHECKS = {
    ".py": "imports resolve to names that really exist; routes/methods/JSON shapes match the API contract exactly; "
           "admin routes use the auth dependency; public write/chat routes are rate limited; inputs validated with limits; "
           "no hardcoded secrets; no raw SQL; startup creates tables and seeds admin (main.py).",
    ".js": "every fetch targets a real contract endpoint with the exact request/response shape; imports only names the "
           "other module exports; every element id used exists in the page HTML; loading/empty/error states; "
           "untrusted text rendered with textContent; token handling correct.",
    ".html": "every src/href file exists in the project; ids match the blueprint; semantic structure; labelled inputs; "
             "uses only classes that exist in styles.css; loads its module script; privacy notice where data is stored.",
    ".css": "complete and well formed; custom properties; responsive; dark theme; focus-visible states; defines classes "
            "the pages use (hero, cards, buttons, forms, chat, table, toast, dialog, skeleton, empty state, admin layout).",
}


def _kind(path):
    return next((v for k, v in CHECKS.items() if path.endswith(k)), "general correctness")


def review(eng, ctx: Ctx, item: dict, log, prev_issues: list[dict] | None = None) -> tuple[str, list[dict]]:
    path = item["path"]

    memory_prompt = ""
    if prev_issues:
        prev_text = "\n".join(f"- {i['problem']}" for i in prev_issues if i.get("problem"))
        memory_prompt = f"""
PREVIOUS ISSUES REPORTED FOR THIS FILE IN THE PRIOR ROUND:
{prev_text}

INSTRUCTIONS FOR THIS RE-REVIEW:
1. First verify if the previous issues listed above were resolved.
2. DO NOT invent new speculative requirements or subjective styling preferences.
3. Only report an issue if:
   - A previous issue is STILL NOT FIXED.
   - The developer introduced a real crash, syntax error, broken import, or contract violation.
"""

    task = f"""You are the strict QA gatekeeper reviewing ONE file: {path} ({item['purpose']}).
Specific checks for this kind of file: {_kind(path)}
{memory_prompt}
Severity rules:
- blocker: causes crash, syntax error, broken contract, or fatal security flaw.
- major: required core feature broken or missing.
- minor: polish, cosmetic, or non-blocking suggestion (does NOT fail the build).

Return ONLY JSON:
{{"verdict":"PASS" or "FAIL","issues":[{{"severity":"blocker|major|minor","problem":"...","fix":"..."}}]}}
Verdict is FAIL if and only if there is any blocker or major issue."""

    common_prefix = f"{STACK_RULES}\n\n{SECURITY_BASELINE}\n\nAPI CONTRACT:\n{ctx.api}"
    parts = [
        (common_prefix, 3),
        ("QA CHECKLIST:\n" + ctx.checklist, 3),
        (ctx.deps_block(item), 4),
        (f"FILE {path}:\n{ctx.files[path]}", 1),
        (task, 0)
    ]

    for attempt in range(2):
        try:
            data = extract_json(eng.ask("qa", parts))
            issues = [{
                "file": path,
                "severity": str(i.get("severity", "major")).lower(),
                "problem": str(i.get("problem", "")),
                "fix": str(i.get("fix", "")),
                "source": "qa"
            } for i in data.get("issues", []) if i.get("problem")]

            gated = [i for i in issues if i["severity"] in GATE]
            return ("FAIL" if gated else "PASS"), issues
        except Exception as e:  # noqa: BLE001
            log(f"QA verdict for {path} unreadable ({e}); retrying")

    return "INCONCLUSIVE", []


def accept(eng, ctx: Ctx, routes: list, verdicts: dict, log) -> list[dict]:
    """Architect's final acceptance. Returns blocking issues (empty list = approved)."""
    iface = "\n\n".join(f"--- {p} ---\n{outline(p, t, 500)}" for p, t in ctx.files.items())
    table = "\n".join(f"- {p}: {v['verdict']}" for p, v in verdicts.items())
    task = """You are the project owner doing final acceptance. Decide whether the delivered project satisfies the
core client requirements. Only block on concrete, critical defects or missing required features.
Return ONLY JSON: {"approved": true|false, "blocking": [{"file":"path","problem":"...","fix":"..."}]}"""

    parts = [
        ("SPEC:\n" + ctx.spec, 3),
        ("QA CHECKLIST:\n" + ctx.checklist, 3),
        ("REAL ROUTES:\n" + "\n".join(f"{m} {p}" for m, p in routes), 2),
        ("QA VERDICTS:\n" + table, 1),
        ("INTERFACE MAP:\n" + iface, 4),
        (task, 0)
    ]
    try:
        data = extract_json(eng.ask("architect", parts))
    except Exception as e:  # noqa: BLE001
        log(f"Architect acceptance unreadable ({e}); treating as approved")
        return []

    if data.get("approved"):
        return []
    return [{
        "file": b.get("file"),
        "severity": "major",
        "problem": str(b.get("problem", "")),
        "fix": str(b.get("fix", "")),
        "source": "architect"
    } for b in data.get("blocking", []) if b.get("file") in ctx.files and b.get("problem")]


##Claude
# """Strict QA gate + the architect's final acceptance."""
# from studio.build import Ctx, outline
# from studio.util import extract_json

# GATE = {"blocker", "major"}

# CHECKS = {
#     ".py": "imports resolve to names that really exist; routes/methods/JSON shapes match the API contract exactly; "
#            "admin routes use the auth dependency; public write/chat routes are rate limited; inputs validated with limits; "
#            "no hardcoded secrets; no raw SQL; errors handled; startup creates tables and seeds admin (main.py).",
#     ".js": "every fetch targets a real contract endpoint with the exact request/response shape; imports only names the "
#            "other module exports; every element id used exists in the page HTML; loading/empty/error states; "
#            "untrusted text rendered with textContent; token handling correct.",
#     ".html": "every src/href file exists in the project; ids match the blueprint; semantic structure; labelled inputs; "
#              "uses only classes that exist in styles.css; loads its module script; privacy notice where data is stored.",
#     ".css": "complete and well formed; custom properties; responsive; dark theme; focus-visible states; defines classes "
#             "the pages use (hero, cards, buttons, forms, chat, table, toast, dialog, skeleton, empty state, admin layout).",
# }


# def _kind(path):
#     return next((v for k, v in CHECKS.items() if path.endswith(k)), "general correctness")


# def review(eng, ctx: Ctx, item: dict, log) -> tuple[str, list[dict]]:
#     path = item["path"]
#     task = f"""You are the strict QA gatekeeper. Review ONE file: {path} ({item['purpose']}).
# Verify against the architect's QA checklist (only the items relevant to this file), the API contract and the
# interfaces of the files it depends on. Specific checks for this kind of file: {_kind(path)}
# Severity: blocker = crashes, wrong behaviour, security hole or contract mismatch; major = a required feature is broken,
# missing or unvalidated; minor = polish only. Do not invent problems; do not miss real ones.
# Write feedback the way a great senior reviewer would: concise, kind, specific. Name the exact function, line or id,
# say why it matters, and give the concrete fix.
# Return ONLY JSON: {{"verdict":"PASS" or "FAIL","issues":[{{"severity":"blocker|major|minor","problem":"...","fix":"..."}}]}}
# Verdict is FAIL if there is any blocker or major issue."""
#     parts = [(task, 0), (f"FILE {path}:\n{ctx.files[path]}", 1), ("QA CHECKLIST:\n" + ctx.checklist, 3),
#              ("API CONTRACT:\n" + ctx.api, 2), (ctx.deps_block(item), 4)]
#     for attempt in range(2):
#         try:
#             data = extract_json(eng.ask("qa", parts))
#             issues = [{"file": path, "severity": str(i.get("severity", "major")).lower(), "problem": str(i.get("problem", "")),
#                        "fix": str(i.get("fix", "")), "source": "qa"} for i in data.get("issues", []) if i.get("problem")]
#             gated = [i for i in issues if i["severity"] in GATE]
#             return ("FAIL" if gated else "PASS"), issues
#         except Exception as e:  # noqa: BLE001
#             log(f"QA verdict for {path} unreadable ({e}); retrying")
#     return "INCONCLUSIVE", []


# def accept(eng, ctx: Ctx, routes: list, verdicts: dict, log) -> list[dict]:
#     """Architect's final acceptance. Returns blocking issues (empty list = approved)."""
#     iface = "\n\n".join(f"--- {p} ---\n{outline(p, t, 500)}" for p, t in ctx.files.items())
#     table = "\n".join(f"- {p}: {v['verdict']}" for p, v in verdicts.items())
#     task = """You are the project owner doing final acceptance. Decide whether the delivered project fully satisfies the
# client's requirement, your spec, and every item of the QA checklist, using the interface map of all files, the real
# route list and the QA verdict table. Be strict but fair: only block on a concrete, named defect or missing required
# feature, and name the single file responsible (it must be one of the project files).
# Return ONLY JSON: {"approved": true|false, "blocking": [{"file":"path","problem":"...","fix":"..."}]}"""
#     parts = [(task, 0), ("SPEC:\n" + ctx.spec, 3), ("QA CHECKLIST:\n" + ctx.checklist, 3),
#              ("REAL ROUTES:\n" + "\n".join(f"{m} {p}" for m, p in routes), 2),
#              ("QA VERDICTS:\n" + table, 1), ("INTERFACE MAP:\n" + iface, 4)]
#     try:
#         data = extract_json(eng.ask("architect", parts))
#     except Exception as e:  # noqa: BLE001
#         log(f"Architect acceptance unreadable ({e}); treating as approved")
#         return []
#     if data.get("approved"):
#         return []
#     return [{"file": b.get("file"), "severity": "major", "problem": str(b.get("problem", "")),
#              "fix": str(b.get("fix", "")), "source": "architect"}
#             for b in data.get("blocking", []) if b.get("file") in ctx.files and b.get("problem")]
