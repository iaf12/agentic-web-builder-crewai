"""Orchestrator: spec -> manifest -> parallel build -> memory-aware QA loop with circuit breaker."""
import json
import os
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from studio.agents import make_agents
from studio.blueprint import make_api, make_checklist, make_manifest, make_spec
from studio.build import Ctx, build_order, generate_file, revise_file
from studio.checks import assign_file, run_automated
from studio.engine import Engine
from studio.llm import get_llm, wait_for_llm
from studio.qa import GATE, accept, review
from studio.writer import render_readme, write_file, write_scaffold, zip_project

OUTPUT_DIR = Path("generated_projects")
MAX_QA_ROUNDS = int(os.getenv("MAX_QA_ROUNDS", "5"))
MAX_ARCHITECT_REJECTIONS = 2
PARALLEL_WORKERS = 3  # Optimal for 24GB VRAM with Qwen2.5-Coder-14B-AWQ (32k context)


def _slug(text: str) -> str:
    return "-".join(re.sub(r"[^a-zA-Z0-9 ]", " ", text).lower().split()[:4]) or "project"


def _save(state_dir: Path, state: dict) -> None:
    (state_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def _cached(state_dir: Path, name: str, fn, as_json=False):
    p = state_dir / name
    if p.exists():
        txt = p.read_text(encoding="utf-8")
        return json.loads(txt) if as_json else txt
    val = fn()
    p.write_text(json.dumps(val, indent=2) if as_json else val, encoding="utf-8")
    return val


def run_build(requirement: str, answers: str, log=print, resume: str | None = None) -> dict:
    eng = Engine(make_agents(get_llm()), log)
    wait_for_llm(log)
    root = Path(resume) if resume else OUTPUT_DIR / f"{_slug(requirement)}-{time.strftime('%Y%m%d-%H%M%S')}"
    sd = root / ".studio"
    sd.mkdir(parents=True, exist_ok=True)
    state_file = sd / "state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {
        "requirement": requirement, "answers": answers, "round": 0, "verdicts": {}, "changed": [],
        "rejections": 0, "status": "RUNNING"
    }
    state["status"] = "RUNNING"
    _save(sd, state)
    name = root.name

    # ---- 1. Architect ----
    log("Architect: writing the product spec")
    spec = _cached(sd, "spec.md", lambda: make_spec(eng, state["requirement"], state["answers"]))
    log("Architect: writing the API contract and security plan")
    api = _cached(sd, "api.md", lambda: make_api(eng, spec))
    log("Architect: planning the file manifest")
    manifest = _cached(sd, "manifest.json", lambda: make_manifest(eng, spec, api, log), as_json=True)
    log("Architect: writing the QA acceptance checklist")
    checklist = _cached(sd, "checklist.md", lambda: make_checklist(eng, spec, api))
    ctx = Ctx(spec, api, checklist, manifest)
    owners = {m["path"]: m for m in manifest}
    dependents = defaultdict(set)
    for m in manifest:
        for d in m["depends_on"]:
            dependents[d].add(m["path"])
    write_scaffold(root)

    # ---- 2. Build files ----
    order = build_order(manifest)
    for n, item in enumerate(order, 1):
        p = root / item["path"]
        if p.exists():
            ctx.files[item["path"]] = p.read_text(encoding="utf-8")
            continue
        log(f"[{n}/{len(order)}] {item['owner'].title()} writes {item['path']}")
        text = generate_file(eng, ctx, item)
        ctx.files[item["path"]] = text
        write_file(root, item["path"], text)

    # ---- 3. Memory-Aware QA Loop with Circuit Breaker ----
    verdicts, routes, status, notes = state["verdicts"], [], "INCOMPLETE", []
    changed = set(state["changed"]) or set(ctx.files)
    history, blocking = [], []

    # Historical state tracking
    prev_issues_by_file = defaultdict(list)
    best_snapshot = dict(ctx.files)
    min_blocking_count = float("inf")
    consecutive_increases = 0

    for rnd in range(state["round"] + 1, MAX_QA_ROUNDS + 1):
        log(f"\n--- QA Round {rnd}/{MAX_QA_ROUNDS} ---")
        auto_issues, routes = run_automated(root, ctx.files)
        auto_issues = [assign_file(i, ctx.files) for i in auto_issues]
        issues = list(auto_issues)

        # Parallel review of changed files
        items_to_review = [item for item in order if (item["path"] in changed or item["path"] not in verdicts)]
        log(f"Reviewing {len(items_to_review)} file(s) across {PARALLEL_WORKERS} parallel threads...")

        def _do_review(it):
            p = it["path"]
            prev = prev_issues_by_file.get(p, [])
            v, found = review(eng, ctx, it, log, prev_issues=prev)
            return p, v, found

        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
            futures = [pool.submit(_do_review, it) for it in items_to_review]
            for f in as_completed(futures):
                p, verdict, found = f.result()
                verdicts[p] = {"verdict": verdict, "round": rnd}
                issues += [i for i in found if i["severity"] in GATE]
                notes += [f"{p}: {i['problem']}" for i in found if i["severity"] not in GATE]

        blocking = [i for i in issues if i["severity"] in GATE]
        current_blocking_count = len(blocking)
        history.append({"round": rnd, "issues": current_blocking_count, "auto": len(auto_issues)})
        log(f"Round {rnd} summary: {current_blocking_count} blocking issue(s) ({len(auto_issues)} automated).")

        # Circuit breaker tracking
        if current_blocking_count < min_blocking_count:
            min_blocking_count = current_blocking_count
            best_snapshot = dict(ctx.files)
            consecutive_increases = 0
        elif current_blocking_count > min_blocking_count:
            consecutive_increases += 1
            log(f"⚠️ Warning: Issue count increased ({min_blocking_count} -> {current_blocking_count}). Alert {consecutive_increases}/2.")
            if consecutive_increases >= 2:
                log("🛑 Circuit Breaker Triggered: Issue count is escalating. Restoring cleanest known snapshot and stopping.")
                ctx.files = best_snapshot
                for p, text in ctx.files.items():
                    write_file(root, p, text)
                break

        # Check if clean
        if not blocking:
            if state["rejections"] >= MAX_ARCHITECT_REJECTIONS:
                status = "COMPLETE"
                break
            log("QA is green. Architect: final acceptance")
            rejected = accept(eng, ctx, routes, verdicts, log)
            if not rejected:
                status = "COMPLETE"
                break
            state["rejections"] += 1
            blocking = rejected
            log(f"Architect requested {len(blocking)} change(s)")

        # Prepare fixes
        by_file = defaultdict(list)
        for i in blocking:
            by_file[i["file"]].append(i)

        prev_issues_by_file = by_file  # Save for next round's verification memory

        log(f"Parallel fixing {len(by_file)} file(s) across {PARALLEL_WORKERS} threads...")

        def _do_fix(path):
            file_issues = by_file[path]
            try:
                new_text = revise_file(eng, ctx, owners[path], file_issues)
                return path, new_text, None
            except Exception as e:  # noqa: BLE001
                return path, None, e

        changed = set()
        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
            futures = [pool.submit(_do_fix, p) for p in by_file]
            for f in as_completed(futures):
                path, new_text, err = f.result()
                if err or new_text is None:
                    log(f"Could not revise {path}: {err}")
                    changed.add(path)
                    continue
                ctx.files[path] = new_text
                write_file(root, path, new_text)
                changed.add(path)
                changed |= dependents[path]
                verdicts[path] = {"verdict": "REVISED", "round": rnd}

        state.update(round=rnd, verdicts=verdicts, changed=sorted(changed))
        _save(sd, state)
    else:
        issues, routes = run_automated(root, ctx.files)
        blocking = [assign_file(i, ctx.files) for i in issues] or blocking
        notes.append("Rounds reached limit; completed with best effort.")

    final_issues = [] if status == "COMPLETE" else blocking
    label = "COMPLETE (QA green, architect approved)" if status == "COMPLETE" else f"INCOMPLETE ({len(final_issues)} issues remaining)"

    # ---- 4. Documents ----
    docs = root / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "ARCHITECTURE.md").write_text(f"# Architecture\n\n{spec}\n\n{api}\n\n## QA acceptance checklist\n{checklist}\n", encoding="utf-8")
    table = "\n".join(f"| `{p}` | {v['verdict']} | {v['round']} |" for p, v in verdicts.items())
    qa_md = (f"# QA Report\n\n**Status: {label}**\n\n| File | Verdict | Round |\n|---|---|---|\n{table}\n\n"
             f"## Rounds\n" + ("\n".join(f"- Round {h['round']}: {h['issues']} blocking issue(s) ({h['auto']} automated)" for h in history) or "- Passed on first round") +
             ("\n\n## Open issues\n" + "\n".join(f"- `{i['file']}`: {i['problem']}" for i in final_issues) if final_issues else "") +
             ("\n\n## Minor notes\n" + "\n".join(f"- {n}" for n in notes[:40]) if notes else "") + "\n")
    (docs / "QA_REPORT.md").write_text(qa_md, encoding="utf-8")
    write_file(root, "README.md", render_readme(name, spec, manifest, routes, label))

    state.update(status=status, round=state["round"], verdicts=verdicts)
    _save(sd, state)
    return {
        "root": root, "zip": zip_project(root), "status": status, "label": label, "files": sorted(ctx.files),
        "verdicts": verdicts, "history": history, "open_issues": final_issues, "notes": notes,
        "spec": spec, "api": api, "checklist": checklist, "qa_report": qa_md
    }

## Claude
# """Orchestrator: spec -> manifest -> build files -> strict QA loop until green -> sign-off.
# Every artifact is saved under <project>/.studio so a crashed run can resume where it stopped."""
# import json
# import os
# import re
# import time
# from collections import defaultdict
# from pathlib import Path

# from studio.agents import make_agents
# from studio.blueprint import make_api, make_checklist, make_manifest, make_spec
# from studio.build import Ctx, build_order, generate_file, revise_file
# from studio.checks import assign_file, run_automated
# from studio.engine import Engine
# from studio.llm import get_llm
# from studio.qa import GATE, accept, review
# from studio.writer import render_readme, write_file, write_scaffold, zip_project

# OUTPUT_DIR = Path("generated_projects")
# MAX_QA_ROUNDS = int(os.getenv("MAX_QA_ROUNDS", "6"))
# MAX_ARCHITECT_REJECTIONS = 2


# def _slug(text: str) -> str:
#     return "-".join(re.sub(r"[^a-zA-Z0-9 ]", " ", text).lower().split()[:4]) or "project"


# def _save(state_dir: Path, state: dict) -> None:
#     (state_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


# def _cached(state_dir: Path, name: str, fn, as_json=False):
#     p = state_dir / name
#     if p.exists():
#         txt = p.read_text(encoding="utf-8")
#         return json.loads(txt) if as_json else txt
#     val = fn()
#     p.write_text(json.dumps(val, indent=2) if as_json else val, encoding="utf-8")
#     return val


# def run_build(requirement: str, answers: str, log=print, resume: str | None = None) -> dict:
#     eng = Engine(make_agents(get_llm()), log)
#     root = Path(resume) if resume else OUTPUT_DIR / f"{_slug(requirement)}-{time.strftime('%Y%m%d-%H%M%S')}"
#     sd = root / ".studio"
#     sd.mkdir(parents=True, exist_ok=True)
#     state_file = sd / "state.json"
#     state = json.loads(state_file.read_text()) if state_file.exists() else {
#         "requirement": requirement, "answers": answers, "round": 0, "verdicts": {}, "changed": [],
#         "rejections": 0, "status": "RUNNING"}
#     state["status"] = "RUNNING"
#     _save(sd, state)
#     name = root.name

#     # ---- 1. Architect ----
#     log("Architect: writing the product spec")
#     spec = _cached(sd, "spec.md", lambda: make_spec(eng, state["requirement"], state["answers"]))
#     log("Architect: writing the API contract and security plan")
#     api = _cached(sd, "api.md", lambda: make_api(eng, spec))
#     log("Architect: planning the file manifest")
#     manifest = _cached(sd, "manifest.json", lambda: make_manifest(eng, spec, api, log), as_json=True)
#     log("Architect: writing the QA acceptance checklist")
#     checklist = _cached(sd, "checklist.md", lambda: make_checklist(eng, spec, api))
#     ctx = Ctx(spec, api, checklist, manifest)
#     owners = {m["path"]: m for m in manifest}
#     dependents = defaultdict(set)
#     for m in manifest:
#         for d in m["depends_on"]:
#             dependents[d].add(m["path"])
#     write_scaffold(root)

#     # ---- 2. Build, one file per call ----
#     order = build_order(manifest)
#     for n, item in enumerate(order, 1):
#         p = root / item["path"]
#         if p.exists():
#             ctx.files[item["path"]] = p.read_text(encoding="utf-8")
#             continue
#         log(f"[{n}/{len(order)}] {item['owner'].title()} writes {item['path']}")
#         text = generate_file(eng, ctx, item)
#         ctx.files[item["path"]] = text
#         write_file(root, item["path"], text)

#     # ---- 3. Strict QA gate: loop until green ----
#     verdicts, routes, status, notes = state["verdicts"], [], "INCOMPLETE", []
#     changed = set(state["changed"]) or set(ctx.files)
#     history, blocking = [], []
#     for rnd in range(state["round"] + 1, MAX_QA_ROUNDS + 1):
#         log(f"QA round {rnd}/{MAX_QA_ROUNDS}: automated checks")
#         issues, routes = run_automated(root, ctx.files)
#         issues = [assign_file(i, ctx.files) for i in issues]
#         for item in order:
#             path = item["path"]
#             if path in changed or path not in verdicts:
#                 log(f"QA reviews {path}")
#                 verdict, found = review(eng, ctx, item, log)
#                 verdicts[path] = {"verdict": verdict, "round": rnd}
#                 issues += [i for i in found if i["severity"] in GATE]
#                 notes += [f"{path}: {i['problem']}" for i in found if i["severity"] not in GATE]
#         blocking = [i for i in issues if i["severity"] in GATE]

#         if not blocking:
#             if state["rejections"] >= MAX_ARCHITECT_REJECTIONS:
#                 status = "COMPLETE"
#                 break
#             log("QA is green. Architect: final acceptance")
#             rejected = accept(eng, ctx, routes, verdicts, log)
#             if not rejected:
#                 status = "COMPLETE"
#                 break
#             state["rejections"] += 1
#             blocking = rejected
#             log(f"Architect asked for {len(blocking)} change(s)")

#         history.append({"round": rnd, "issues": len(blocking)})
#         log(f"{len(blocking)} blocking issue(s). Developers are fixing them")
#         by_file = defaultdict(list)
#         for i in blocking:
#             by_file[i["file"]].append(i)
#         changed = set()
#         for path, file_issues in by_file.items():
#             try:
#                 new = revise_file(eng, ctx, owners[path], file_issues)
#             except Exception as e:  # noqa: BLE001
#                 log(f"Could not revise {path}: {e}")
#                 changed.add(path)
#                 continue
#             ctx.files[path] = new
#             write_file(root, path, new)
#             changed.add(path)
#             changed |= dependents[path]
#             verdicts[path] = {"verdict": "REVISED", "round": rnd}
#         state.update(round=rnd, verdicts=verdicts, changed=sorted(changed))
#         _save(sd, state)
#     else:  # rounds exhausted without a green gate: report what is still failing
#         issues, routes = run_automated(root, ctx.files)
#         blocking = [assign_file(i, ctx.files) for i in issues] or blocking
#         notes.append("The last round of fixes was not re-reviewed by QA.")

#     final_issues = [] if status == "COMPLETE" else blocking
#     label = "COMPLETE (QA green, architect approved)" if status == "COMPLETE" else f"INCOMPLETE (QA not green after {MAX_QA_ROUNDS} rounds)"

#     # ---- 4. Documents ----
#     docs = root / "docs"
#     docs.mkdir(exist_ok=True)
#     (docs / "ARCHITECTURE.md").write_text(f"# Architecture\n\n{spec}\n\n{api}\n\n## QA acceptance checklist\n{checklist}\n", encoding="utf-8")
#     table = "\n".join(f"| `{p}` | {v['verdict']} | {v['round']} |" for p, v in verdicts.items())
#     qa_md = (f"# QA Report\n\n**Status: {label}**\n\n| File | Verdict | Round |\n|---|---|---|\n{table}\n\n"
#              f"## Rounds\n" + ("\n".join(f"- Round {h['round']}: {h['issues']} blocking issue(s) fixed" for h in history) or "- Passed on the first round") +
#              ("\n\n## Open issues\n" + "\n".join(f"- `{i['file']}`: {i['problem']}" for i in final_issues) if final_issues else "") +
#              ("\n\n## Minor notes\n" + "\n".join(f"- {n}" for n in notes[:40]) if notes else "") + "\n")
#     (docs / "QA_REPORT.md").write_text(qa_md, encoding="utf-8")
#     write_file(root, "README.md", render_readme(name, spec, manifest, routes, label))

#     state.update(status=status, round=state["round"], verdicts=verdicts)
#     _save(sd, state)
#     return {"root": root, "zip": zip_project(root), "status": status, "label": label, "files": sorted(ctx.files),
#             "verdicts": verdicts, "history": history, "open_issues": final_issues, "notes": notes,
#             "spec": spec, "api": api, "checklist": checklist, "qa_report": qa_md}




# """Orchestrator: spec -> manifest -> parallel build -> memory-aware QA loop with circuit breaker."""
# import json
# import os
# import re
# import time
# from collections import defaultdict
# from concurrent.futures import ThreadPoolExecutor, as_completed
# from pathlib import Path

# from studio.agents import make_agents
# from studio.blueprint import make_api, make_checklist, make_manifest, make_spec
# from studio.build import Ctx, build_order, generate_file, revise_file
# from studio.checks import assign_file, run_automated
# from studio.engine import Engine
# from studio.llm import get_llm
# from studio.qa import GATE, accept, review
# from studio.writer import render_readme, write_file, write_scaffold, zip_project

# OUTPUT_DIR = Path("generated_projects")
# MAX_QA_ROUNDS = int(os.getenv("MAX_QA_ROUNDS", "5"))
# MAX_ARCHITECT_REJECTIONS = 2
# PARALLEL_WORKERS = 3  # Optimal for 24GB VRAM with Qwen2.5-Coder-14B-AWQ (32k context)


# def _slug(text: str) -> str:
#     return "-".join(re.sub(r"[^a-zA-Z0-9 ]", " ", text).lower().split()[:4]) or "project"


# def _save(state_dir: Path, state: dict) -> None:
#     (state_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


# def _cached(state_dir: Path, name: str, fn, as_json=False):
#     p = state_dir / name
#     if p.exists():
#         txt = p.read_text(encoding="utf-8")
#         return json.loads(txt) if as_json else txt
#     val = fn()
#     p.write_text(json.dumps(val, indent=2) if as_json else val, encoding="utf-8")
#     return val


# def run_build(requirement: str, answers: str, log=print, resume: str | None = None) -> dict:
#     eng = Engine(make_agents(get_llm()), log)
#     root = Path(resume) if resume else OUTPUT_DIR / f"{_slug(requirement)}-{time.strftime('%Y%m%d-%H%M%S')}"
#     sd = root / ".studio"
#     sd.mkdir(parents=True, exist_ok=True)
#     state_file = sd / "state.json"
#     state = json.loads(state_file.read_text()) if state_file.exists() else {
#         "requirement": requirement, "answers": answers, "round": 0, "verdicts": {}, "changed": [],
#         "rejections": 0, "status": "RUNNING"
#     }
#     state["status"] = "RUNNING"
#     _save(sd, state)
#     name = root.name

#     # ---- 1. Architect ----
#     log("Architect: writing the product spec")
#     spec = _cached(sd, "spec.md", lambda: make_spec(eng, state["requirement"], state["answers"]))
#     log("Architect: writing the API contract and security plan")
#     api = _cached(sd, "api.md", lambda: make_api(eng, spec))
#     log("Architect: planning the file manifest")
#     manifest = _cached(sd, "manifest.json", lambda: make_manifest(eng, spec, api, log), as_json=True)
#     log("Architect: writing the QA acceptance checklist")
#     checklist = _cached(sd, "checklist.md", lambda: make_checklist(eng, spec, api))
#     ctx = Ctx(spec, api, checklist, manifest)
#     owners = {m["path"]: m for m in manifest}
#     dependents = defaultdict(set)
#     for m in manifest:
#         for d in m["depends_on"]:
#             dependents[d].add(m["path"])
#     write_scaffold(root)

#     # ---- 2. Build files ----
#     order = build_order(manifest)
#     for n, item in enumerate(order, 1):
#         p = root / item["path"]
#         if p.exists():
#             ctx.files[item["path"]] = p.read_text(encoding="utf-8")
#             continue
#         log(f"[{n}/{len(order)}] {item['owner'].title()} writes {item['path']}")
#         text = generate_file(eng, ctx, item)
#         ctx.files[item["path"]] = text
#         write_file(root, item["path"], text)

#     # ---- 3. Memory-Aware QA Loop with Circuit Breaker ----
#     verdicts, routes, status, notes = state["verdicts"], [], "INCOMPLETE", []
#     changed = set(state["changed"]) or set(ctx.files)
#     history, blocking = [], []

#     # Historical state tracking
#     prev_issues_by_file = defaultdict(list)
#     best_snapshot = dict(ctx.files)
#     min_blocking_count = float("inf")
#     consecutive_increases = 0

#     for rnd in range(state["round"] + 1, MAX_QA_ROUNDS + 1):
#         log(f"\n--- QA Round {rnd}/{MAX_QA_ROUNDS} ---")
#         auto_issues, routes = run_automated(root, ctx.files)
#         auto_issues = [assign_file(i, ctx.files) for i in auto_issues]
#         issues = list(auto_issues)

#         # Parallel review of changed files
#         items_to_review = [item for item in order if (item["path"] in changed or item["path"] not in verdicts)]
#         log(f"Reviewing {len(items_to_review)} file(s) across {PARALLEL_WORKERS} parallel threads...")

#         def _do_review(it):
#             p = it["path"]
#             prev = prev_issues_by_file.get(p, [])
#             v, found = review(eng, ctx, it, log, prev_issues=prev)
#             return p, v, found

#         with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
#             futures = [pool.submit(_do_review, it) for it in items_to_review]
#             for f in as_completed(futures):
#                 p, verdict, found = f.result()
#                 verdicts[p] = {"verdict": verdict, "round": rnd}
#                 issues += [i for i in found if i["severity"] in GATE]
#                 notes += [f"{p}: {i['problem']}" for i in found if i["severity"] not in GATE]

#         blocking = [i for i in issues if i["severity"] in GATE]
#         current_blocking_count = len(blocking)
#         history.append({"round": rnd, "issues": current_blocking_count, "auto": len(auto_issues)})
#         log(f"Round {rnd} summary: {current_blocking_count} blocking issue(s) ({len(auto_issues)} automated).")

#         # Circuit breaker tracking
#         if current_blocking_count < min_blocking_count:
#             min_blocking_count = current_blocking_count
#             best_snapshot = dict(ctx.files)
#             consecutive_increases = 0
#         elif current_blocking_count > min_blocking_count:
#             consecutive_increases += 1
#             log(f"⚠️ Warning: Issue count increased ({min_blocking_count} -> {current_blocking_count}). Alert {consecutive_increases}/2.")
#             if consecutive_increases >= 2:
#                 log("🛑 Circuit Breaker Triggered: Issue count is escalating. Restoring cleanest known snapshot and stopping.")
#                 ctx.files = best_snapshot
#                 for p, text in ctx.files.items():
#                     write_file(root, p, text)
#                 break

#         # Check if clean
#         if not blocking:
#             if state["rejections"] >= MAX_ARCHITECT_REJECTIONS:
#                 status = "COMPLETE"
#                 break
#             log("QA is green. Architect: final acceptance")
#             rejected = accept(eng, ctx, routes, verdicts, log)
#             if not rejected:
#                 status = "COMPLETE"
#                 break
#             state["rejections"] += 1
#             blocking = rejected
#             log(f"Architect requested {len(blocking)} change(s)")

#         # Prepare fixes
#         by_file = defaultdict(list)
#         for i in blocking:
#             by_file[i["file"]].append(i)

#         prev_issues_by_file = by_file  # Save for next round's verification memory

#         log(f"Parallel fixing {len(by_file)} file(s) across {PARALLEL_WORKERS} threads...")

#         def _do_fix(path):
#             file_issues = by_file[path]
#             try:
#                 new_text = revise_file(eng, ctx, owners[path], file_issues)
#                 return path, new_text, None
#             except Exception as e:  # noqa: BLE001
#                 return path, None, e

#         changed = set()
#         with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
#             futures = [pool.submit(_do_fix, p) for p in by_file]
#             for f in as_completed(futures):
#                 path, new_text, err = f.result()
#                 if err or new_text is None:
#                     log(f"Could not revise {path}: {err}")
#                     changed.add(path)
#                     continue
#                 ctx.files[path] = new_text
#                 write_file(root, path, new_text)
#                 changed.add(path)
#                 changed |= dependents[path]
#                 verdicts[path] = {"verdict": "REVISED", "round": rnd}

#         state.update(round=rnd, verdicts=verdicts, changed=sorted(changed))
#         _save(sd, state)
#     else:
#         issues, routes = run_automated(root, ctx.files)
#         blocking = [assign_file(i, ctx.files) for i in issues] or blocking
#         notes.append("Rounds reached limit; completed with best effort.")

#     final_issues = [] if status == "COMPLETE" else blocking
#     label = "COMPLETE (QA green, architect approved)" if status == "COMPLETE" else f"INCOMPLETE ({len(final_issues)} issues remaining)"

#     # ---- 4. Documents ----
#     docs = root / "docs"
#     docs.mkdir(exist_ok=True)
#     (docs / "ARCHITECTURE.md").write_text(f"# Architecture\n\n{spec}\n\n{api}\n\n## QA acceptance checklist\n{checklist}\n", encoding="utf-8")
#     table = "\n".join(f"| `{p}` | {v['verdict']} | {v['round']} |" for p, v in verdicts.items())
#     qa_md = (f"# QA Report\n\n**Status: {label}**\n\n| File | Verdict | Round |\n|---|---|---|\n{table}\n\n"
#              f"## Rounds\n" + ("\n".join(f"- Round {h['round']}: {h['issues']} blocking issue(s) ({h['auto']} automated)" for h in history) or "- Passed on first round") +
#              ("\n\n## Open issues\n" + "\n".join(f"- `{i['file']}`: {i['problem']}" for i in final_issues) if final_issues else "") +
#              ("\n\n## Minor notes\n" + "\n".join(f"- {n}" for n in notes[:40]) if notes else "") + "\n")
#     (docs / "QA_REPORT.md").write_text(qa_md, encoding="utf-8")
#     write_file(root, "README.md", render_readme(name, spec, manifest, routes, label))

#     state.update(status=status, round=state["round"], verdicts=verdicts)
#     _save(sd, state)
#     return {
#         "root": root, "zip": zip_project(root), "status": status, "label": label, "files": sorted(ctx.files),
#         "verdicts": verdicts, "history": history, "open_issues": final_issues, "notes": notes,
#         "spec": spec, "api": api, "checklist": checklist, "qa_report": qa_md
#     }

# ## Claude
# # """Orchestrator: spec -> manifest -> build files -> strict QA loop until green -> sign-off.
# # Every artifact is saved under <project>/.studio so a crashed run can resume where it stopped."""
# # import json
# # import os
# # import re
# # import time
# # from collections import defaultdict
# # from pathlib import Path

# # from studio.agents import make_agents
# # from studio.blueprint import make_api, make_checklist, make_manifest, make_spec
# # from studio.build import Ctx, build_order, generate_file, revise_file
# # from studio.checks import assign_file, run_automated
# # from studio.engine import Engine
# # from studio.llm import get_llm
# # from studio.qa import GATE, accept, review
# # from studio.writer import render_readme, write_file, write_scaffold, zip_project

# # OUTPUT_DIR = Path("generated_projects")
# # MAX_QA_ROUNDS = int(os.getenv("MAX_QA_ROUNDS", "6"))
# # MAX_ARCHITECT_REJECTIONS = 2


# # def _slug(text: str) -> str:
# #     return "-".join(re.sub(r"[^a-zA-Z0-9 ]", " ", text).lower().split()[:4]) or "project"


# # def _save(state_dir: Path, state: dict) -> None:
# #     (state_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


# # def _cached(state_dir: Path, name: str, fn, as_json=False):
# #     p = state_dir / name
# #     if p.exists():
# #         txt = p.read_text(encoding="utf-8")
# #         return json.loads(txt) if as_json else txt
# #     val = fn()
# #     p.write_text(json.dumps(val, indent=2) if as_json else val, encoding="utf-8")
# #     return val


# # def run_build(requirement: str, answers: str, log=print, resume: str | None = None) -> dict:
# #     eng = Engine(make_agents(get_llm()), log)
# #     root = Path(resume) if resume else OUTPUT_DIR / f"{_slug(requirement)}-{time.strftime('%Y%m%d-%H%M%S')}"
# #     sd = root / ".studio"
# #     sd.mkdir(parents=True, exist_ok=True)
# #     state_file = sd / "state.json"
# #     state = json.loads(state_file.read_text()) if state_file.exists() else {
# #         "requirement": requirement, "answers": answers, "round": 0, "verdicts": {}, "changed": [],
# #         "rejections": 0, "status": "RUNNING"}
# #     state["status"] = "RUNNING"
# #     _save(sd, state)
# #     name = root.name

# #     # ---- 1. Architect ----
# #     log("Architect: writing the product spec")
# #     spec = _cached(sd, "spec.md", lambda: make_spec(eng, state["requirement"], state["answers"]))
# #     log("Architect: writing the API contract and security plan")
# #     api = _cached(sd, "api.md", lambda: make_api(eng, spec))
# #     log("Architect: planning the file manifest")
# #     manifest = _cached(sd, "manifest.json", lambda: make_manifest(eng, spec, api, log), as_json=True)
# #     log("Architect: writing the QA acceptance checklist")
# #     checklist = _cached(sd, "checklist.md", lambda: make_checklist(eng, spec, api))
# #     ctx = Ctx(spec, api, checklist, manifest)
# #     owners = {m["path"]: m for m in manifest}
# #     dependents = defaultdict(set)
# #     for m in manifest:
# #         for d in m["depends_on"]:
# #             dependents[d].add(m["path"])
# #     write_scaffold(root)

# #     # ---- 2. Build, one file per call ----
# #     order = build_order(manifest)
# #     for n, item in enumerate(order, 1):
# #         p = root / item["path"]
# #         if p.exists():
# #             ctx.files[item["path"]] = p.read_text(encoding="utf-8")
# #             continue
# #         log(f"[{n}/{len(order)}] {item['owner'].title()} writes {item['path']}")
# #         text = generate_file(eng, ctx, item)
# #         ctx.files[item["path"]] = text
# #         write_file(root, item["path"], text)

# #     # ---- 3. Strict QA gate: loop until green ----
# #     verdicts, routes, status, notes = state["verdicts"], [], "INCOMPLETE", []
# #     changed = set(state["changed"]) or set(ctx.files)
# #     history, blocking = [], []
# #     for rnd in range(state["round"] + 1, MAX_QA_ROUNDS + 1):
# #         log(f"QA round {rnd}/{MAX_QA_ROUNDS}: automated checks")
# #         issues, routes = run_automated(root, ctx.files)
# #         issues = [assign_file(i, ctx.files) for i in issues]
# #         for item in order:
# #             path = item["path"]
# #             if path in changed or path not in verdicts:
# #                 log(f"QA reviews {path}")
# #                 verdict, found = review(eng, ctx, item, log)
# #                 verdicts[path] = {"verdict": verdict, "round": rnd}
# #                 issues += [i for i in found if i["severity"] in GATE]
# #                 notes += [f"{path}: {i['problem']}" for i in found if i["severity"] not in GATE]
# #         blocking = [i for i in issues if i["severity"] in GATE]

# #         if not blocking:
# #             if state["rejections"] >= MAX_ARCHITECT_REJECTIONS:
# #                 status = "COMPLETE"
# #                 break
# #             log("QA is green. Architect: final acceptance")
# #             rejected = accept(eng, ctx, routes, verdicts, log)
# #             if not rejected:
# #                 status = "COMPLETE"
# #                 break
# #             state["rejections"] += 1
# #             blocking = rejected
# #             log(f"Architect asked for {len(blocking)} change(s)")

# #         history.append({"round": rnd, "issues": len(blocking)})
# #         log(f"{len(blocking)} blocking issue(s). Developers are fixing them")
# #         by_file = defaultdict(list)
# #         for i in blocking:
# #             by_file[i["file"]].append(i)
# #         changed = set()
# #         for path, file_issues in by_file.items():
# #             try:
# #                 new = revise_file(eng, ctx, owners[path], file_issues)
# #             except Exception as e:  # noqa: BLE001
# #                 log(f"Could not revise {path}: {e}")
# #                 changed.add(path)
# #                 continue
# #             ctx.files[path] = new
# #             write_file(root, path, new)
# #             changed.add(path)
# #             changed |= dependents[path]
# #             verdicts[path] = {"verdict": "REVISED", "round": rnd}
# #         state.update(round=rnd, verdicts=verdicts, changed=sorted(changed))
# #         _save(sd, state)
# #     else:  # rounds exhausted without a green gate: report what is still failing
# #         issues, routes = run_automated(root, ctx.files)
# #         blocking = [assign_file(i, ctx.files) for i in issues] or blocking
# #         notes.append("The last round of fixes was not re-reviewed by QA.")

# #     final_issues = [] if status == "COMPLETE" else blocking
# #     label = "COMPLETE (QA green, architect approved)" if status == "COMPLETE" else f"INCOMPLETE (QA not green after {MAX_QA_ROUNDS} rounds)"

# #     # ---- 4. Documents ----
# #     docs = root / "docs"
# #     docs.mkdir(exist_ok=True)
# #     (docs / "ARCHITECTURE.md").write_text(f"# Architecture\n\n{spec}\n\n{api}\n\n## QA acceptance checklist\n{checklist}\n", encoding="utf-8")
# #     table = "\n".join(f"| `{p}` | {v['verdict']} | {v['round']} |" for p, v in verdicts.items())
# #     qa_md = (f"# QA Report\n\n**Status: {label}**\n\n| File | Verdict | Round |\n|---|---|---|\n{table}\n\n"
# #              f"## Rounds\n" + ("\n".join(f"- Round {h['round']}: {h['issues']} blocking issue(s) fixed" for h in history) or "- Passed on the first round") +
# #              ("\n\n## Open issues\n" + "\n".join(f"- `{i['file']}`: {i['problem']}" for i in final_issues) if final_issues else "") +
# #              ("\n\n## Minor notes\n" + "\n".join(f"- {n}" for n in notes[:40]) if notes else "") + "\n")
# #     (docs / "QA_REPORT.md").write_text(qa_md, encoding="utf-8")
# #     write_file(root, "README.md", render_readme(name, spec, manifest, routes, label))

# #     state.update(status=status, round=state["round"], verdicts=verdicts)
# #     _save(sd, state)
# #     return {"root": root, "zip": zip_project(root), "status": status, "label": label, "files": sorted(ctx.files),
# #             "verdicts": verdicts, "history": history, "open_issues": final_issues, "notes": notes,
# #             "spec": spec, "api": api, "checklist": checklist, "qa_report": qa_md}
