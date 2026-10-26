# Crew Web Studio

A multi-agent platform (CrewAI) that turns a plain-English requirement into a complete, runnable website.
It does not stop until a strict QA gate is green and the architect signs off.

## Run it

**1. Start the model on RunPod (vLLM)**
```bash
pip install vllm
vllm serve Qwen/Qwen2.5-Coder-14B-Instruct-AWQ --host 0.0.0.0 --port 8000 --max-model-len 32768
```
Expose HTTP port 8000. Test: `curl https://<POD_ID>-8000.proxy.runpod.net/v1/models`

**2. Start the platform**
```bash
python -m venv .venv && source .venv/bin/activate     # Python 3.10 - 3.12
pip install -r requirements.txt
cp .env.example .env        # set LLM_BASE_URL and LLM_MODEL
streamlit run app.py
```

## Project map
```
crew_web_studio/
├── app.py                  # Streamlit UI: requirement -> questions -> live build log -> download
├── requirements.txt
├── .env.example            # model endpoint, context size, QA rounds
└── studio/
    ├── llm.py              # connects CrewAI to your vLLM server
    ├── agents.py           # the 5 agents: architect, designer, backend, frontend, QA
    ├── prompts.py          # fixed tech stack, security baseline, modern-practice rules, file format
    ├── clarify.py          # architect interview: questions + suggested answers
    ├── blueprint.py        # architect output: spec, API contract, file manifest, QA checklist
    ├── build.py            # developers: ONE file per call, interface outlines, cut-off recovery
    ├── checks.py           # non-LLM checks: syntax, imports, names, file refs, app boot, API routes
    ├── pipeline.py         # orchestrator: build -> QA loop until green -> docs -> zip, with resume
    ├── engine.py           # every LLM call: budgeted, retried, stateless
    ├── budget.py           # context budget manager (token estimate, trim, assemble)
    ├── writer.py           # scaffold files, project-map README, zip
    └── util.py             # tolerant JSON extraction
```

## How a build works
```
Requirement ──▶ Architect interview (you confirm answers)
                    │
                    ▼
   Architect: spec ─▶ API contract ─▶ file manifest ─▶ QA checklist     (saved in .studio/)
                    │
                    ▼
   Designer ─▶ Backend ─▶ Frontend      one file per LLM call, each sees only
   (styles.css) (models, routers, main)  the blueprint + interfaces of its dependencies
                    │
                    ▼
   ┌──────────────── QA LOOP (max MAX_QA_ROUNDS) ────────────────┐
   │ 1. Automated checks: syntax, imports, names exist, file      │
   │    references, app really boots, every fetch() hits a route  │
   │ 2. QA reviews each changed file against the checklist        │
   │ 3. Blocker/major issues? owners fix their file with QA's     │
   │    feedback, dependents are re-reviewed, go to 1             │
   │ 4. Green? Architect does final acceptance (may send back)    │
   └──────────────────────────────────────────────────────────────┘
                    │
                    ▼
   COMPLETE only if QA is green AND the architect approves.
   Otherwise the project is flagged INCOMPLETE with the open issues listed.
```

## Staying inside the context window
The 32k window is protected at six levels, so a long build cannot overflow near the end:

1. **Stateless steps.** No step receives earlier agent outputs. Each prompt is built fresh from files saved on disk.
2. **One file per call.** Output always fits `LLM_MAX_TOKENS`; the model only needs the blueprint and the *interfaces* (signatures, exports, CSS class names) of the files it depends on, never their full code.
3. **Budget manager.** `budget.py` assembles every prompt in priority order and trims the least important parts (neighbour code first) so prompt + answer always fit.
4. **Cut-off recovery.** If an answer is truncated, the engine asks the model to continue from the last lines, then verifies Python files parse.
5. **Auto-shrink and retry.** On a context error the prompt is rebuilt at 65% size; on network errors it retries with backoff.
6. **Checkpoint and resume.** Every artifact and every QA round is saved in `<project>/.studio/`. If anything stops, press **Resume** in the sidebar and it continues from the last step. Builds also run in a background thread, so refreshing the browser does not kill them.

## Settings (.env)
| Variable | Default | Meaning |
|---|---|---|
| LLM_BASE_URL | none | vLLM OpenAI endpoint ending in /v1 |
| LLM_MODEL | openai/Qwen/Qwen2.5-Coder-14B-Instruct-AWQ | "openai/" + served model name |
| LLM_CONTEXT | 32768 | Must equal vLLM `--max-model-len` |
| LLM_MAX_TOKENS | 6000 | Reserved for each answer |
| MAX_QA_ROUNDS | 6 | QA fix rounds before the build is flagged INCOMPLETE |
