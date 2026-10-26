import os
import time

import httpx
from crewai import LLM


def _served_name() -> str:
    return os.getenv("LLM_MODEL", "openai/Qwen/Qwen2.5-Coder-14B-Instruct-AWQ").removeprefix("openai/")


def wait_for_llm(log=print, timeout: int = 900) -> None:
    """Block until the vLLM server answers /models (it can take minutes to load and compile after a pod start).
    Fails fast with a clear message on a wrong API key or a wrong model name."""
    base = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
    key = os.getenv("LLM_API_KEY", "EMPTY")
    deadline, told, last = time.time() + timeout, False, "no answer"
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=10)
            if r.status_code == 200:
                ids = [m.get("id") for m in r.json().get("data", [])]
                if ids and _served_name() not in ids:
                    raise RuntimeError(f"Model '{_served_name()}' is not served. The server offers: {ids}. "
                                       "Fix LLM_MODEL in .env (openai/ + --served-model-name).")
                if told:
                    log("Model server is ready.")
                return
            if r.status_code in (401, 403):
                raise RuntimeError(f"Model server rejected LLM_API_KEY (HTTP {r.status_code}). "
                                   "It must equal the vLLM --api-key value.")
            last = f"HTTP {r.status_code}"
        except (httpx.HTTPError, ValueError) as e:
            last = type(e).__name__
        if not told:
            log("Waiting for the model server to finish starting (vLLM is still loading)...")
            told = True
        time.sleep(5)
    raise RuntimeError(f"Model server at {base} was not ready after {timeout}s (last: {last}).")


def get_llm(temperature: float = 0.2) -> LLM:
    """Qwen2.5-Coder served by vLLM on RunPod through an OpenAI-compatible API."""
    base_url = os.getenv("LLM_BASE_URL", "").strip()
    if not base_url or "<" in base_url:
        raise RuntimeError("LLM_BASE_URL is not set. Copy .env.example to .env and fill it in.")
    return LLM(
        model=os.getenv("LLM_MODEL", "openai/Qwen/Qwen2.5-Coder-14B-Instruct-AWQ"),
        base_url=base_url,
        api_key=os.getenv("LLM_API_KEY", "EMPTY"),
        temperature=temperature,
        max_tokens=int(os.getenv("LLM_MAX_TOKENS", "6000")),
        timeout=900,
    )



# import os

# from crewai import LLM


# def get_llm(temperature: float = 0.2) -> LLM:
#     """Qwen2.5-Coder served by vLLM on RunPod through an OpenAI-compatible API."""
#     base_url = os.getenv("LLM_BASE_URL", "").strip()
#     if not base_url or "<" in base_url:
#         raise RuntimeError("LLM_BASE_URL is not set. Copy .env.example to .env and fill it in.")
#     return LLM(
#         model=os.getenv("LLM_MODEL", "openai/Qwen/Qwen2.5-Coder-14B-Instruct-AWQ"),
#         base_url=base_url,
#         api_key=os.getenv("LLM_API_KEY", "EMPTY"),
#         temperature=temperature,
#         max_tokens=int(os.getenv("LLM_MAX_TOKENS", "6000")),
#         timeout=900,
#     )
