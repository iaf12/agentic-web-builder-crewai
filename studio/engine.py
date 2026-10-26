"""One place where every LLM call goes through: budgeted, retried, stateless, thread-safe."""
import copy
import threading
import time

from crewai import Crew, Process, Task

from studio.budget import assemble, prompt_budget

CONTEXT_HINTS = (
    "context length", "context window", "maximum context", "too long",
    "reduce the length", "input_tokens", "contextwindowexceeded"
)


class Engine:
    def __init__(self, agents: dict, log=print):
        self.agents = agents
        self.log = log
        self._lock = threading.Lock()

    def _safe_log(self, msg: str):
        with self._lock:
            self.log(msg)

    def ask(self, who: str, parts: list[tuple[str, int]], expected: str = "Exactly what the instructions ask for.") -> str:
        # Create an isolated copy of the agent per request to allow safe multi-threading
        agent = copy.copy(self.agents[who])
        scale = 1.0
        last_err = None

        for attempt in range(8):
            prompt = assemble(parts, prompt_budget(scale))
            try:
                task = Task(description=prompt, expected_output=expected, agent=agent)
                out = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False).kickoff()
                res = getattr(out, "raw", None) or str(out)
                if res and res.strip():
                    return res
                raise ValueError("Model returned empty response.")
            except Exception as e:  # noqa: BLE001
                last_err = e
                msg = (type(e).__name__ + " " + str(e)).lower()
                if any(h in msg for h in CONTEXT_HINTS):
                    scale *= 0.65
                    self._safe_log(f"Context too large for {who}, rebuilding prompt at {int(scale * 100)}% size (attempt {attempt + 1})")
                else:
                    wait = min(60, 5 * 2 ** attempt)   # 5, 10, 20, 40, 60... (server may still be warming up)
                    self._safe_log(f"LLM call for {who} failed ({type(e).__name__}: {str(e)[:200]}); "
                                   f"retrying in {wait}s (attempt {attempt + 1}/8)")
                    time.sleep(wait)

        raise RuntimeError(f"LLM call for {who} failed after retries: {last_err}")




# """One place where every LLM call goes through: budgeted, retried, stateless, thread-safe."""
# import copy
# import threading
# import time

# from crewai import Crew, Process, Task

# from studio.budget import assemble, prompt_budget

# CONTEXT_HINTS = (
#     "context length", "context window", "maximum context", "too long",
#     "reduce the length", "input_tokens", "contextwindowexceeded"
# )


# class Engine:
#     def __init__(self, agents: dict, log=print):
#         self.agents = agents
#         self.log = log
#         self._lock = threading.Lock()

#     def _safe_log(self, msg: str):
#         with self._lock:
#             self.log(msg)

#     def ask(self, who: str, parts: list[tuple[str, int]], expected: str = "Exactly what the instructions ask for.") -> str:
#         # Create an isolated copy of the agent per request to allow safe multi-threading
#         agent = copy.copy(self.agents[who])
#         scale = 1.0
#         last_err = None

#         for attempt in range(5):
#             prompt = assemble(parts, prompt_budget(scale))
#             try:
#                 task = Task(description=prompt, expected_output=expected, agent=agent)
#                 out = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False).kickoff()
#                 res = getattr(out, "raw", None) or str(out)
#                 if res and res.strip():
#                     return res
#                 raise ValueError("Model returned empty response.")
#             except Exception as e:  # noqa: BLE001
#                 last_err = e
#                 msg = (type(e).__name__ + " " + str(e)).lower()
#                 if any(h in msg for h in CONTEXT_HINTS):
#                     scale *= 0.65
#                     self._safe_log(f"Context too large for {who}, rebuilding prompt at {int(scale * 100)}% size (attempt {attempt + 1})")
#                 else:
#                     wait = 2 * (attempt + 1)
#                     self._safe_log(f"LLM call for {who} failed ({type(e).__name__}); retrying in {wait}s")
#                     time.sleep(wait)

#         raise RuntimeError(f"LLM call for {who} failed after retries: {last_err}")

# # Claude
# # """One place where every LLM call goes through: budgeted, retried, stateless."""
# # import time

# # from crewai import Crew, Process, Task

# # from studio.budget import assemble, prompt_budget

# # CONTEXT_HINTS = ("context length", "context window", "maximum context", "too long",
# #                  "reduce the length", "input_tokens", "contextwindowexceeded")


# # class Engine:
# #     def __init__(self, agents: dict, log=print):
# #         self.agents = agents
# #         self.log = log

# #     def ask(self, who: str, parts: list[tuple[str, int]], expected: str = "Exactly what the instructions ask for.") -> str:
# #         agent = self.agents[who]
# #         scale = 1.0
# #         last_err = None
# #         for attempt in range(5):
# #             prompt = assemble(parts, prompt_budget(scale))
# #             try:
# #                 task = Task(description=prompt, expected_output=expected, agent=agent)
# #                 out = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False).kickoff()
# #                 return getattr(out, "raw", None) or str(out)
# #             except Exception as e:  # noqa: BLE001
# #                 last_err = e
# #                 msg = (type(e).__name__ + " " + str(e)).lower()
# #                 if any(h in msg for h in CONTEXT_HINTS):
# #                     scale *= 0.65
# #                     self.log(f"Context too large, rebuilding the prompt at {int(scale * 100)}% size (attempt {attempt + 1})")
# #                 else:
# #                     wait = 3 * (2 ** attempt)
# #                     self.log(f"LLM call failed ({type(e).__name__}); retrying in {wait}s")
# #                     time.sleep(wait)
# #         raise RuntimeError(f"LLM call failed after retries: {last_err}")
