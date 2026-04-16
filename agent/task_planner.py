"""
agent/task_planner.py  –  Groq + Llama-3.3 converts NL → precise browser task.
"""
import os
from groq import Groq

PANEL_URL = os.getenv("PANEL_URL", "http://localhost:5000")

SYSTEM = f"""You are an IT admin assistant converting natural-language requests into
step-by-step browser navigation instructions for an AI browser agent.

The IT Admin Panel lives at {PANEL_URL}:
  /          Dashboard
  /users     Create User (name, email, role: employee/admin/manager)
             Reset Password (email)
             Enable/Disable User (email)
             Table of all users (columns: Name, Email, Role, License, Status)
  /licenses  Assign License (email, license_type: basic/pro/enterprise)
             Table of assigned licenses

RULES:
- Always navigate to the correct page first.
- For conditional tasks ("check if X exists, if not create them") include:
    "Go to /users. Look at the users table. If EMAIL is NOT in the table, then…"
- Reference form fields by their visible label names.
- Never use dev tools, console, or direct API calls.
- Return plain-text numbered steps only. No markdown.
"""


def plan_task(request: str) -> str:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GROQ_API_KEY. Add it to .env or set it in your shell before starting the panel.")

    client = Groq(api_key=api_key)
    r = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role":"system","content":SYSTEM},
            {"role":"user","content":request},
        ],
        temperature=0.2,
        max_tokens=600,
    )
    steps = r.choices[0].message.content.strip()
    print(f"\n[Planner] Task plan:\n{steps}\n{'─'*50}")
    return steps
