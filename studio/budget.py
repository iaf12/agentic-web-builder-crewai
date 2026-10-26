"""Context budget control: every prompt is assembled to fit the model window."""
import os

CONTEXT = int(os.getenv("LLM_CONTEXT", "32768"))      # model window (vLLM --max-model-len)
MAX_OUT = int(os.getenv("LLM_MAX_TOKENS", "6000"))    # tokens reserved for the answer
OVERHEAD = 2000                                        # CrewAI wrapper + safety margin


def est_tokens(text: str) -> int:
    return int(len(text) / 3.0) + 1                    # conservative for code


def prompt_budget(scale: float = 1.0) -> int:
    return int((CONTEXT - MAX_OUT - OVERHEAD) * scale)


def trim(text: str, max_tokens: int) -> str:
    marker = "\n...[trimmed to fit context]...\n"
    limit = max(200, max_tokens) * 3
    if len(text) <= limit:
        return text
    keep = limit - len(marker)
    head = int(keep * 0.7)
    return text[:head] + marker + text[-(keep - head):]


def assemble(parts: list[tuple[str, int]], budget: int) -> str:
    """parts = [(text, shrink_rank)]. Highest rank is trimmed first; rank 0 text is kept intact
    unless nothing else can shrink."""
    items = [[t, r] for t, r in parts if t]
    for _ in range(20):
        total = sum(est_tokens(t) for t, _ in items)
        if total <= budget:
            break
        over = total - budget
        cands = [p for p in items if est_tokens(p[0]) > 400]
        if not cands:
            break
        c = max(cands, key=lambda p: (p[1], est_tokens(p[0])))
        c[0] = trim(c[0], max(300, est_tokens(c[0]) - over))
    return "\n\n".join(t for t, _ in items)
