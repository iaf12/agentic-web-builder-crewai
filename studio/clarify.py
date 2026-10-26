import json

SYSTEM = (
    "You are a principal software architect interviewing a client before building their website. "
    "Your job is to uncover everything needed to build a good, secure project."
)

PROMPT = """CLIENT REQUIREMENT:
{requirement}

Write 6 to 10 short questions the client must answer. Cover what applies: target users, pages/features,
what data must be stored, whether an admin dashboard is needed, whether the admin should see user
conversations/submissions, login/roles, privacy and security, branding/tone/language, external
integrations (AI chatbot, email), and deployment.
For each question give a sensible suggested answer the client can accept or edit.
Return ONLY a JSON array, no markdown, in this shape:
[{{"question": "...", "why": "one short reason", "suggested": "..."}}]"""

FALLBACK = [
    {"question": "Who are the target users and what is the main goal of the site?", "why": "Shapes pages and tone.", "suggested": "General public looking for reliable information."},
    {"question": "Do you need an admin dashboard?", "why": "Lets you manage content and data.", "suggested": "Yes, login-protected."},
    {"question": "Should the admin see what users type into the chatbot or forms?", "why": "Affects storage and privacy.", "suggested": "Yes, store conversations and show them in the dashboard with a privacy notice."},
    {"question": "What data must be stored in the database?", "why": "Defines the data model.", "suggested": "Contact messages, chat logs, and managed content entries."},
    {"question": "Any branding, language or colour preferences?", "why": "Guides the UI designer.", "suggested": "Clean, professional, English."},
    {"question": "Do you need an AI chatbot?", "why": "Needs an LLM endpoint.", "suggested": "Yes, answering questions about the site's topic."},
]


def generate_questions(requirement: str, llm) -> list[dict]:
    try:
        raw = llm.call([
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": PROMPT.format(requirement=requirement)},
        ])
        raw = str(raw)
        data = json.loads(raw[raw.index("["): raw.rindex("]") + 1])
        items = [
            {"question": str(d["question"]), "why": str(d.get("why", "")), "suggested": str(d.get("suggested", ""))}
            for d in data if isinstance(d, dict) and d.get("question")
        ]
        return items[:10] or FALLBACK
    except Exception:
        return FALLBACK
