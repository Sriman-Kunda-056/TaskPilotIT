"""
agent/orchestrator.py  –  Pipeline: NL → Groq plan → Playwright + Gemini browser agent
"""
import os
import sqlite3
from agent.task_planner import plan_task
from agent.browser_agent import run_browser_agent

PANEL_URL = os.getenv("PANEL_URL", "http://localhost:5000")


def _get_user_context(db_path: str) -> str:
    """Query the users table so the planner can resolve names → emails directly."""
    if not db_path:
        return ""
    try:
        c = sqlite3.connect(db_path)
        rows = c.execute(
            "SELECT name, email, active FROM users ORDER BY name"
        ).fetchall()
        c.close()
        if not rows:
            return ""
        lines = ["Users currently in the system (use these exact emails in the plan):"]
        for name, email, active in rows:
            status = "active" if active else "disabled"
            lines.append(f"  {name} | {email} | {status}")
        return "\n".join(lines)
    except Exception:
        return ""


async def run_task(
    natural_language_request: str,
    headless: bool = True,
    run_id: int = 0,
    db_path: str = "",
    screenshots_dir: str = "",
) -> str:
    print(f"\n{'='*55}\n[Orchestrator] {natural_language_request}\n{'='*55}")

    # Pass current user list so planner can use real emails (avoids vision steps)
    user_context = _get_user_context(db_path)
    task_steps = plan_task(natural_language_request, user_context=user_context)

    result = await run_browser_agent(
        task=task_steps,
        run_id=run_id,
        db_path=db_path,
        screenshots_dir=screenshots_dir,
        headless=headless,
    )

    print(f"[Orchestrator] Done ✅  Result: {result[:120]}")
    return result
