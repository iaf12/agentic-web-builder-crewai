import os

os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

import json
import threading
import time
import traceback
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from studio.clarify import generate_questions  # noqa: E402
from studio.llm import get_llm  # noqa: E402
from studio.pipeline import OUTPUT_DIR, run_build  # noqa: E402

st.set_page_config(page_title="Crew Web Studio", page_icon="🛠️", layout="wide")


@st.cache_resource
def jobs() -> dict:
    """Build jobs live in the server process, so a browser refresh does not kill a build."""
    return {}


class Job:
    def __init__(self, requirement, answers, resume=None):
        self.requirement, self.answers, self.resume = requirement, answers, resume
        self.log, self.result, self.error, self.done = [], None, None, False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def write(self, msg):
        self.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def _run(self):
        try:
            self.result = run_build(self.requirement, self.answers, self.write, self.resume)
        except Exception:  # noqa: BLE001
            self.error = traceback.format_exc()
        self.done = True


def unfinished_runs():
    out = []
    for f in sorted(OUTPUT_DIR.glob("*/.studio/state.json")):
        try:
            s = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        root = f.parent.parent
        busy = any(j.resume == str(root) and not j.done for j in jobs().values())
        if s.get("status") != "COMPLETE" and not busy:
            out.append((root, s))
    return out


ss = st.session_state
for k, v in (("stage", "input"), ("requirement", ""), ("questions", []), ("job_id", None)):
    ss.setdefault(k, v)

st.title("🛠️ Crew Web Studio")
st.caption("Architect → Designer → Backend → Frontend → strict QA loop → Architect sign-off. Powered by CrewAI + your vLLM model.")

with st.sidebar:
    st.subheader("Model")
    st.write("Endpoint:", os.getenv("LLM_BASE_URL", "not set"))
    st.write("Model:", os.getenv("LLM_MODEL", "not set"))
    st.write("Context:", os.getenv("LLM_CONTEXT", "32768"), "tokens")
    st.divider()
    runs = unfinished_runs()
    if runs:
        st.subheader("Resume an interrupted build")
        for root, s in runs:
            if st.button(f"Resume {root.name}", key=str(root)):
                jid = str(time.time())
                jobs()[jid] = Job(s["requirement"], s["answers"], resume=str(root))
                ss.job_id, ss.stage = jid, "building"
                st.rerun()
    if st.button("Start over"):
        ss.stage, ss.requirement, ss.questions, ss.job_id = "input", "", [], None
        st.rerun()

if ss.stage == "input":
    st.header("1. Describe the website you need")
    text = st.text_area("Requirement", height=180, value=ss.requirement,
                        placeholder="Example: A law literature website with a chatbot that answers questions about legal texts.")
    up = st.file_uploader("...or upload a requirement file (.txt / .md)", type=["txt", "md"])
    if up is not None:
        text = up.read().decode("utf-8", errors="ignore")
        st.info("Requirement loaded from file.")
    if st.button("Analyze requirement", type="primary", disabled=not text.strip()):
        ss.requirement = text.strip()
        try:
            with st.spinner("The architect is preparing questions..."):
                ss.questions = generate_questions(ss.requirement, get_llm(temperature=0.3))
            ss.stage = "questions"
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(str(e))

elif ss.stage == "questions":
    st.header("2. Architect's questions")
    st.write("The architect suggests answers. Edit anything you like, then build.")
    st.info(ss.requirement)
    answers = []
    for i, q in enumerate(ss.questions):
        a = st.text_area(f"{i + 1}. {q['question']}", value=q["suggested"], key=f"ans_{i}", height=80, help=q.get("why") or None)
        answers.append(f"Q: {q['question']}\nA: {a}")
    extra = st.text_area("Anything else the architect should know?", key="extra", height=80)
    if extra.strip():
        answers.append(f"Q: Additional notes\nA: {extra.strip()}")
    if st.button("Build the website", type="primary"):
        jid = str(time.time())
        jobs()[jid] = Job(ss.requirement, "\n\n".join(answers))
        ss.job_id, ss.stage = jid, "building"
        st.rerun()

elif ss.stage == "building":
    job = jobs().get(ss.job_id)
    st.header("3. Building")
    if job is None:
        st.warning("This build is no longer tracked. Use Resume in the sidebar.")
    else:
        st.caption("The build runs in the background. You can refresh the page; it keeps going.")
        st.code("\n".join(job.log[-40:]) or "Starting...", language="text")
        if job.done:
            if job.error:
                st.error("The build stopped with an error. Use Resume in the sidebar to continue from the last saved step.")
                st.code(job.error)
            else:
                ss.stage = "done"
                st.rerun()
        else:
            time.sleep(2)
            st.rerun()

elif ss.stage == "done":
    r = jobs()[ss.job_id].result
    st.header("4. Your project")
    (st.success if r["status"] == "COMPLETE" else st.warning)(r["label"])
    st.write(f"Saved in: `{r['root']}`")
    with open(r["zip"], "rb") as f:
        st.download_button("Download project (.zip)", f, file_name=r["zip"].name, mime="application/zip")
    st.code(f"cd {r['root']}\npip install -r requirements.txt\ncp .env.example .env\npython run.py", language="bash")
    if r["open_issues"]:
        st.subheader("Still failing")
        for i in r["open_issues"]:
            st.code(f"{i['file']}: {i['problem']}")
    t1, t2, t3, t4 = st.tabs(["QA report", "Architecture", "Checklist", "Files"])
    t1.markdown(r["qa_report"])
    t2.markdown(r["spec"] + "\n\n" + r["api"])
    t3.markdown(r["checklist"])
    t4.write("\n".join(f"- `{p}`" for p in r["files"]))
