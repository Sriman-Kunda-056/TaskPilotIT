"""
agent/task_planner.py  –  Groq + Llama converts NL → structured JSON plan.

Each step is one of:
  {"action":"navigate","url":"/path"}
  {"action":"fill","selector":"css","value":"text"}
  {"action":"select","selector":"css","value":"option"}
  {"action":"click","selector":"css"}
  {"action":"delete-user","email":"user@example.com"}
  {"action":"vision","instruction":"what to visually find/do"}

Direct steps (navigate/fill/select/click/delete-user) run with zero Gemini calls.
Vision steps call Gemini only when visual inspection is genuinely needed.
"""
import json
import os
import re
from groq import Groq

PANEL_URL = os.getenv("PANEL_URL", "http://localhost:5000")

SYSTEM = f"""You are an IT admin assistant. Convert natural-language requests into a structured JSON plan for a browser automation agent.

The IT Admin Panel lives at {PANEL_URL}. Known pages and CSS selectors:

  /licenses — Assign License:
    input[name="email"]              — User Email
    select[name="license_type"]      — License Type (values: basic, pro, enterprise)
    button[type="submit"]            — Assign button

  /users — User Management:
    Create User form:
      form[action="/users/create"] input[name="name"]    — Full Name
      form[action="/users/create"] input[name="email"]   — Email
      form[action="/users/create"] select[name="role"]   — Role (employee/admin/manager)
      form[action="/users/create"] button[type="submit"] — Create User button

    Reset Password form:
      form[action="/users/reset-password"] input[name="email"]   — User Email
      form[action="/users/reset-password"] button[type="submit"] — Reset Password button

    Toggle Enable/Disable form:
      form[action="/users/toggle"] input[name="email"]   — User Email
      form[action="/users/toggle"] button[type="submit"] — Toggle button

Available step types:
  {{"action":"navigate","url":"/path"}}
  {{"action":"fill","selector":"css-selector","value":"text"}}
  {{"action":"select","selector":"css-selector","value":"option-value"}}
  {{"action":"click","selector":"css-selector"}}
  {{"action":"delete-user","email":"user@example.com"}}   ← deletes user by email, NO vision needed
  {{"action":"vision","instruction":"what to visually find and do on screen"}}

RULES:
- Use navigate/fill/select/click/delete-user for ALL deterministic steps — they run without AI vision (saves quota).
- Use a vision step ONLY when you truly cannot determine the action without seeing the screen.
- For delete/remove tasks: use {{"action":"delete-user","email":"..."}} — NEVER use a vision step for deletion when you know the email.
- If the user list is provided above, use those exact emails. Do NOT guess email addresses.
- If a requested user is NOT in the provided list, output a single vision step explaining the user was not found.
- Output ONLY a valid JSON array — no markdown, no explanation, nothing else.

Example — "assign pro license to john@company.com and delete carol@company.com":
[
  {{"action":"navigate","url":"/licenses"}},
  {{"action":"fill","selector":"input[name='email']","value":"john@company.com"}},
  {{"action":"select","selector":"select[name='license_type']","value":"pro"}},
  {{"action":"click","selector":"button[type='submit']"}},
  {{"action":"delete-user","email":"carol@company.com"}}
]"""


def plan_task(request: str, user_context: str = "") -> str:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        print("[Planner] GROQ_API_KEY missing; using raw task.")
        return request

    try:
        client = Groq(api_key=api_key)

        user_msg = f"{user_context}\n\nTask: {request}" if user_context else request

        r = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
            max_tokens=1000,
        )
        raw = r.choices[0].message.content.strip()

        # Extract the JSON array (handles markdown fences or leading/trailing text)
        match = re.search(r'\[[\s\S]*\]', raw)
        if not match:
            raise ValueError("No JSON array found in response")

        steps = json.loads(match.group())
        if not isinstance(steps, list):
            raise ValueError("Parsed value is not a list")

        n_vision = sum(1 for s in steps if s.get("action") == "vision")
        n_direct = len(steps) - n_vision
        result = json.dumps(steps)
        print(
            f"\n[Planner] {len(steps)}-step plan "
            f"({n_direct} direct, {n_vision} vision):\n"
            f"{json.dumps(steps, indent=2)}\n{'─'*50}"
        )
        return result

    except Exception as e:
        print(f"[Planner] Structured planning failed ({e}); falling back to raw task.")
        return request
