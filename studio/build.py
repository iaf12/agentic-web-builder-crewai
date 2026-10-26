"""Developers' work: write and revise files with small, relevant context and surgical patching."""
import ast
import re
from dataclasses import dataclass, field

from studio.budget import est_tokens
from studio.prompts import FILE_FORMAT, PATCH_FORMAT, ROLE_RULES, SECURITY_BASELINE, STACK_RULES
from studio.writer import _strip_fence

HEADER_RE = re.compile(r"=== FILE: ([^\n]+?) ===\r?\n")
END = "=== END FILE ==="
OWNER_ORDER = {"designer": 0, "backend": 1, "frontend": 2}

PATCH_BLOCK_RE = re.compile(
    r"<<<<<<< SEARCH\r?\n(.*?)\r?\n=======\r?\n(.*?)\r?\n>>>>>>> REPLACE",
    re.DOTALL
)


@dataclass
class Ctx:
    spec: str
    api: str
    checklist: str
    manifest: list
    files: dict = field(default_factory=dict)

    def item(self, path):
        return next(m for m in self.manifest if m["path"] == path)

    def tree(self):
        return "\n".join(f"- {m['path']} ({m['owner']}): {m['purpose']}" for m in self.manifest)

    def deps_block(self, item) -> str:
        out = []
        for d in item["depends_on"]:
            if d in self.files:
                out.append(f"--- interface of {d} ---\n{outline(d, self.files[d])}")
        return "\n\n".join(out)


def build_order(manifest: list) -> list:
    by = {m["path"]: m for m in manifest}
    key = lambda m: (OWNER_ORDER[m["owner"]], m["path"] == "backend/main.py", m["path"] != "frontend/js/common.js", m["path"])
    ordered, seen = [], set()

    def visit(m, stack=()):
        if m["path"] in seen or m["path"] in stack:
            return
        for d in m["depends_on"]:
            visit(by[d], stack + (m["path"],))
        seen.add(m["path"])
        ordered.append(m)

    for m in sorted(manifest, key=key):
        visit(m)
    return ordered


def outline(path: str, text: str, full_limit: int = 1400) -> str:
    if est_tokens(text) <= full_limit:
        return text
    if path.endswith(".py"):
        return _py_outline(text)
    if path.endswith(".js"):
        keep = [l.rstrip() for l in text.splitlines() if re.match(r"\s*(export|async function|function|const \w+ = (async )?\()", l)]
        return "\n".join(keep[:150]) or text[:3000]
    if path.endswith(".css"):
        classes = sorted(set(re.findall(r"\.([a-zA-Z][\w-]*)", text)))
        variables = sorted(set(re.findall(r"(--[\w-]+)\s*:", text)))
        return "CSS classes: " + " ".join(classes) + "\nCSS variables: " + " ".join(variables)
    return text[: full_limit * 3]


def _sig(n) -> str:
    deco = "".join(f"@{ast.unparse(d)}\n" for d in n.decorator_list)
    ret = f" -> {ast.unparse(n.returns)}" if n.returns else ""
    return f"{deco}{'async ' if isinstance(n, ast.AsyncFunctionDef) else ''}def {n.name}({ast.unparse(n.args)}){ret}"


def _py_outline(text: str) -> str:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text[:3000]
    out = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(_sig(n))
        elif isinstance(n, ast.ClassDef):
            out.append(f"class {n.name}({', '.join(ast.unparse(b) for b in n.bases)}):")
            for it in n.body:
                if isinstance(it, ast.AnnAssign):
                    out.append("    " + ast.unparse(it)[:140])
                elif isinstance(it, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append("    " + _sig(it).replace("\n", "\n    "))
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            out.append(ast.unparse(n)[:160])
    return "\n".join(out)


def extract_file(text: str) -> tuple[str, bool]:
    m = HEADER_RE.search(text or "")
    if m:
        rest = text[m.end():]
        if END in rest:
            return _strip_fence(rest.split(END)[0]), True
        return _strip_fence(rest), False
    fence = re.search(r"```[\w+-]*\n(.*?)```", text or "", re.S)
    if fence:
        return fence.group(1), True
    return _strip_fence(text or ""), False


def apply_patches(original: str, patch_text: str) -> tuple[str, bool]:
    """Applies SEARCH/REPLACE blocks. Returns (updated_text, success)."""
    blocks = list(PATCH_BLOCK_RE.finditer(patch_text or ""))
    if not blocks:
        return original, False

    current = original
    applied = 0
    for b in blocks:
        search_block = b.group(1)
        replace_block = b.group(2)
        if search_block in current:
            current = current.replace(search_block, replace_block, 1)
            applied += 1
        else:
            # Tolerant match: ignore trailing whitespace differences per line
            search_lines = [line.rstrip() for line in search_block.splitlines()]
            curr_lines = current.splitlines()
            matched = False
            for i in range(len(curr_lines) - len(search_lines) + 1):
                if [l.rstrip() for l in curr_lines[i : i + len(search_lines)]] == search_lines:
                    prefix = "\n".join(curr_lines[:i])
                    suffix = "\n".join(curr_lines[i + len(search_lines):])
                    current = (prefix + ("\n" if prefix else "") + replace_block + ("\n" if suffix else "") + suffix)
                    applied += 1
                    matched = True
                    break
    return current, (applied > 0)


def _finish(eng, who, path, text) -> str:
    body, complete = extract_file(text)
    tries = 0
    while not complete and tries < 3:
        lines = body.splitlines()
        partial = lines.pop() if lines else ""
        tail = "\n".join(lines[-25:])
        more = eng.ask(who, [(f"Your previous answer for {path} was cut off. The last complete lines were:\n{tail}\n\n"
                              f"The next line began with: {partial[:120]}\n"
                              f"Continue the file starting with that line written out in full, then the rest. "
                              f"Output only the remaining lines, then a final line `{END}`. No header, no fences.", 0)])
        body = "\n".join(lines) + "\n" + _strip_fence(more.split(END)[0])
        complete = END in more
        tries += 1
    body = body.rstrip() + "\n"

    if path.endswith(".py"):
        for _ in range(2):
            try:
                ast.parse(body)
                break
            except SyntaxError as e:
                fixed = eng.ask(who, [(f"This Python file has a syntax error at line {e.lineno}: {e.msg}. "
                                       f"Fix it and output the complete corrected file.\n"
                                       + FILE_FORMAT.format(path=path), 0), (body, 1)])
                nb, _ = extract_file(fixed)
                body = nb.rstrip() + "\n"
    return body


def generate_file(eng, ctx: Ctx, item: dict) -> str:
    path, who = item["path"], item["owner"]
    # Shared context first -> activates vLLM prefix cache across all files
    common_prefix = f"{STACK_RULES}\n\n{SECURITY_BASELINE}\n\nSPEC:\n{ctx.spec}\n\nAPI CONTRACT:\n{ctx.api}"
    task = (f"YOU ARE WRITING EXACTLY ONE FILE.\nPATH: {path}\nPURPOSE: {item['purpose']}\n\n{ROLE_RULES[who]}\n\n"
            "Implement it completely according to the contract and interfaces below.\n" + FILE_FORMAT.format(path=path))
    parts = [
        (common_prefix, 3),
        (ROLE_RULES[who], 2),
        ("PROJECT FILES:\n" + ctx.tree(), 1),
        (ctx.deps_block(item), 4),
        (task, 0)
    ]
    return _finish(eng, who, path, eng.ask(who, parts))


def revise_file(eng, ctx: Ctx, item: dict, issues: list[dict]) -> str:
    path, who = item["path"], item["owner"]
    original_code = ctx.files[path]

    fb = "\n".join(f"{i + 1}. [{x['severity'].upper()}] {x['problem']}" + (f"\n   Suggested fix: {x['fix']}" if x.get("fix") else "")
                   for i, x in enumerate(issues))

    task = (
        f"QA REVIEWED {path} AND FOUND THESE ISSUES:\n{fb}\n\n"
        "Fix the issues while preserving all existing working code.\n"
        "Prefer returning SEARCH/REPLACE blocks. If you must rewrite the whole file, use the full file format.\n"
        + PATCH_FORMAT.format(path=path)
    )

    common_prefix = f"{STACK_RULES}\n\nAPI CONTRACT:\n{ctx.api}"
    parts = [
        (common_prefix, 3),
        ("CURRENT FILE " + path + ":\n" + original_code, 1),
        (ctx.deps_block(item), 4),
        (task, 0)
    ]

    response = eng.ask(who, parts)

    # 1. Try applying surgical SEARCH/REPLACE patches
    patched_code, success = apply_patches(original_code, response)
    if success:
        if path.endswith(".py"):
            try:
                ast.parse(patched_code)
                return patched_code.rstrip() + "\n"
            except SyntaxError:
                pass  # Fall back to whole-file extraction
        else:
            return patched_code.rstrip() + "\n"

    # 2. Fallback: extract complete file
    revised = _finish(eng, who, path, response)

    # If Python syntax is broken by rewrite, safeguard by returning original
    if path.endswith(".py"):
        try:
            ast.parse(revised)
        except SyntaxError:
            return original_code

    return revised


## Claude
# """Developers' work: write one file per LLM call, with small, relevant context."""
# import ast
# import re
# from dataclasses import dataclass, field

# from studio.budget import est_tokens
# from studio.prompts import FILE_FORMAT, ROLE_RULES
# from studio.writer import _strip_fence

# HEADER_RE = re.compile(r"=== FILE: ([^\n]+?) ===\r?\n")
# END = "=== END FILE ==="
# OWNER_ORDER = {"designer": 0, "backend": 1, "frontend": 2}


# @dataclass
# class Ctx:
#     spec: str
#     api: str
#     checklist: str
#     manifest: list
#     files: dict = field(default_factory=dict)

#     def item(self, path):
#         return next(m for m in self.manifest if m["path"] == path)

#     def tree(self):
#         return "\n".join(f"- {m['path']} ({m['owner']}): {m['purpose']}" for m in self.manifest)

#     def deps_block(self, item) -> str:
#         out = []
#         for d in item["depends_on"]:
#             if d in self.files:
#                 out.append(f"--- interface of {d} ---\n{outline(d, self.files[d])}")
#         return "\n\n".join(out)


# # ---------- ordering ----------
# def build_order(manifest: list) -> list:
#     by = {m["path"]: m for m in manifest}
#     key = lambda m: (OWNER_ORDER[m["owner"]], m["path"] == "backend/main.py", m["path"] != "frontend/js/common.js", m["path"])
#     ordered, seen = [], set()

#     def visit(m, stack=()):
#         if m["path"] in seen or m["path"] in stack:
#             return
#         for d in m["depends_on"]:
#             visit(by[d], stack + (m["path"],))
#         seen.add(m["path"])
#         ordered.append(m)

#     for m in sorted(manifest, key=key):
#         visit(m)
#     return ordered


# # ---------- interfaces (what neighbours need to know, in few tokens) ----------
# def outline(path: str, text: str, full_limit: int = 1400) -> str:
#     if est_tokens(text) <= full_limit:
#         return text
#     if path.endswith(".py"):
#         return _py_outline(text)
#     if path.endswith(".js"):
#         keep = [l.rstrip() for l in text.splitlines() if re.match(r"\s*(export|async function|function|const \w+ = (async )?\()", l)]
#         return "\n".join(keep[:150]) or text[:3000]
#     if path.endswith(".css"):
#         classes = sorted(set(re.findall(r"\.([a-zA-Z][\w-]*)", text)))
#         variables = sorted(set(re.findall(r"(--[\w-]+)\s*:", text)))
#         return "CSS classes: " + " ".join(classes) + "\nCSS variables: " + " ".join(variables)
#     return text[: full_limit * 3]


# def _sig(n) -> str:
#     deco = "".join(f"@{ast.unparse(d)}\n" for d in n.decorator_list)
#     ret = f" -> {ast.unparse(n.returns)}" if n.returns else ""
#     return f"{deco}{'async ' if isinstance(n, ast.AsyncFunctionDef) else ''}def {n.name}({ast.unparse(n.args)}){ret}"


# def _py_outline(text: str) -> str:
#     try:
#         tree = ast.parse(text)
#     except SyntaxError:
#         return text[:3000]
#     out = []
#     for n in tree.body:
#         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
#             out.append(_sig(n))
#         elif isinstance(n, ast.ClassDef):
#             out.append(f"class {n.name}({', '.join(ast.unparse(b) for b in n.bases)}):")
#             for it in n.body:
#                 if isinstance(it, ast.AnnAssign):
#                     out.append("    " + ast.unparse(it)[:140])
#                 elif isinstance(it, (ast.FunctionDef, ast.AsyncFunctionDef)):
#                     out.append("    " + _sig(it).replace("\n", "\n    "))
#         elif isinstance(n, (ast.Assign, ast.AnnAssign)):
#             out.append(ast.unparse(n)[:160])
#     return "\n".join(out)


# # ---------- extraction / truncation handling ----------
# def extract_file(text: str) -> tuple[str, bool]:
#     """Return (content, complete). Handles missing END marker (cut-off) and bare code fences."""
#     m = HEADER_RE.search(text or "")
#     if m:
#         rest = text[m.end():]
#         if END in rest:
#             return _strip_fence(rest.split(END)[0]), True
#         return _strip_fence(rest), False
#     fence = re.search(r"```[\w+-]*\n(.*?)```", text or "", re.S)
#     if fence:
#         return fence.group(1), True
#     return _strip_fence(text or ""), False


# def _finish(eng, who, path, text) -> str:
#     body, complete = extract_file(text)
#     tries = 0
#     while not complete and tries < 4:
#         lines = body.splitlines()
#         partial = lines.pop() if lines else ""          # last line may be cut mid-way: drop it, model rewrites it
#         tail = "\n".join(lines[-25:])
#         more = eng.ask(who, [(f"Your previous answer for {path} was cut off. The last complete lines were:\n{tail}\n\n"
#                               f"The next line began with: {partial[:120]}\n"
#                               f"Continue the file starting with that line written out in full, then the rest. "
#                               f"Output only the remaining lines, then a final line `{END}`. No header, no fences.", 0)])
#         body = "\n".join(lines) + "\n" + _strip_fence(more.split(END)[0])
#         complete = END in more
#         tries += 1
#     body = body.rstrip() + "\n"
#     if path.endswith(".py"):
#         for _ in range(2):
#             try:
#                 ast.parse(body)
#                 break
#             except SyntaxError as e:
#                 fixed = eng.ask(who, [(f"This Python file has a syntax error at line {e.lineno}: {e.msg}. "
#                                        f"Fix it and output the complete corrected file.\n"
#                                        + FILE_FORMAT.format(path=path), 0), (body, 1)])
#                 nb, _ = extract_file(fixed)
#                 body = nb.rstrip() + "\n"
#     return body


# # ---------- write / revise ----------
# def generate_file(eng, ctx: Ctx, item: dict) -> str:
#     path, who = item["path"], item["owner"]
#     task = (f"YOU ARE WRITING EXACTLY ONE FILE.\nPATH: {path}\nPURPOSE: {item['purpose']}\n\n{ROLE_RULES[who]}\n\n"
#             "Implement it completely so it matches the architect's blueprint, the API contract and the interfaces "
#             "of the files it depends on (given below). Use exactly the names, routes, element ids, CSS classes and "
#             "fields they define. Compact, readable code with brief comments.\n" + FILE_FORMAT.format(path=path))
#     parts = [(task, 0), ("PROJECT FILES:\n" + ctx.tree(), 1), ("API CONTRACT:\n" + ctx.api, 2),
#              ("SPEC:\n" + ctx.spec, 3), (ctx.deps_block(item), 4)]
#     return _finish(eng, who, path, eng.ask(who, parts))


# def revise_file(eng, ctx: Ctx, item: dict, issues: list[dict]) -> str:
#     path, who = item["path"], item["owner"]
#     fb = "\n".join(f"{i + 1}. [{x['severity']}] {x['problem']}" + (f"\n   Suggested fix: {x['fix']}" if x.get("fix") else "")
#                    for i, x in enumerate(issues))
#     task = (f"QA REVIEWED YOUR FILE {path} AND FOUND THESE PROBLEMS:\n{fb}\n\n"
#             "Fix every problem at its root cause without breaking anything else, keep everything that already works, "
#             "and keep names/routes/ids consistent with the blueprint and the interfaces below. "
#             "Output the COMPLETE corrected file.\n" + FILE_FORMAT.format(path=path))
#     parts = [(task, 0), ("CURRENT FILE " + path + ":\n" + ctx.files[path], 1), ("API CONTRACT:\n" + ctx.api, 2),
#              ("SPEC:\n" + ctx.spec, 3), (ctx.deps_block(item), 4)]
#     return _finish(eng, who, path, eng.ask(who, parts))
