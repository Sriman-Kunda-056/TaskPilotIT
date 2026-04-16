"""
agent/orchestrator.py  –  Pipeline: NL → plan → browser → confirm via WS.
"""
import asyncio, os
from agent.task_planner  import plan_task
from agent.browser_agent import run_browser_agent
from agent.ws_listener   import PanelEventListener

PANEL_URL = os.getenv("PANEL_URL", "http://localhost:5000")


async def run_task(
    natural_language_request: str,
    headless: bool = True,
    run_id: int = 0,
    sock=None,           # Flask-SocketIO instance (passed from panel/app.py)
) -> str:
    print(f"\n{'='*55}\n[Orchestrator] {natural_language_request}\n{'='*55}")

    # 1. WebSocket listener (confirm panel actions)
    ws = PanelEventListener(PANEL_URL)
    ws.start()

    # 2. Plan (Groq, free)
    task_steps = plan_task(natural_language_request)

    # 3. If we have a SocketIO instance, emit the plan as a step
    if sock:
        sock.emit("agent_step", {
            "run_id": run_id,
            "step_num": 0,
            "description": f"Plan ready ({task_steps.count(chr(10))+1} steps) — launching browser",
            "status": "running",
        })

    # 4. Run browser agent (Gemini Flash, free)
    result = await run_browser_agent(
        task=task_steps,
        run_id=run_id,
        sock=sock,
        headless=headless,
    )

    ws.stop()
    print(f"[Orchestrator] Done ✅  Result: {result[:120]}")
    return result
