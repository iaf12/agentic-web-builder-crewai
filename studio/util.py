import json
import re


def extract_json(text: str):
    """Pull the first JSON array/object out of a model answer (tolerates prose and code fences)."""
    text = re.sub(r"```(?:json)?", "", text or "")
    starts = [i for i in (text.find("["), text.find("{")) if i != -1]
    if not starts:
        raise ValueError("no JSON found")
    i = min(starts)
    closer = "]" if text[i] == "[" else "}"
    j = text.rfind(closer)
    if j <= i:
        raise ValueError("unterminated JSON")
    return json.loads(text[i: j + 1])
