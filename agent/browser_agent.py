"""
agent/browser_agent.py
Browser Use agent with live screenshot streaming back to the panel via SocketIO.
Each step: navigate → screenshot → emit to /agent page.
"""

import asyncio
import base64
import os
from typing import Optional, Any

from browser_use import Agent, Browser, BrowserConfig
from langchain_google_genai import ChatGoogleGenerativeAI


async def run_browser_agent(
    task: str,
    run_id: int,
    sock: Optional[Any],     # Flask-SocketIO instance for emitting events
    headless: bool = True,
) -> str:
    llm = ChatGoogleGenerativeAI(
        model="Gemini 3.1 Flash  preview",
        google_api_key=os.environ["GEMINI_API_KEY"],
        temperature=0.1,
    )

    browser = Browser(
        config=BrowserConfig(
            headless=headless,
            extra_chromium_args=["--window-size=1280,900"],
        )
    )

    step_counter = {"n": 0}

    # ── Screenshot helper ──────────────────────────────────────────────────
    async def capture_and_emit(description: str, status: str = "running"):
        """Take a screenshot of the current browser page and broadcast it."""
        step_counter["n"] += 1
        step_num = step_counter["n"]

        # Emit step description first (fast)
        if sock is not None:
            sock.emit("agent_step", {
                "run_id":      run_id,
                "step_num":    step_num,
                "description": description,
                "status":      status,
            })

        # Take screenshot
        try:
            context = browser.browser_context
            if context:
                pages = context.pages if hasattr(context, "pages") else []
                page  = pages[-1] if pages else None
                if page is None and hasattr(context, "new_page"):
                    page = await context.new_page()
                if page:
                    png_bytes = await page.screenshot(full_page=False)
                    b64 = base64.b64encode(png_bytes).decode()
                    if sock is not None:
                        sock.emit("agent_screenshot", {
                            "run_id":   run_id,
                            "step_num": step_num,
                            "image":    b64,
                        })
        except Exception as e:
            print(f"[Screenshot] Could not capture: {e}")

    # ── Build agent with step hooks ────────────────────────────────────────
    agent = Agent(
        task=task,
        llm=llm,
        browser=browser,
        max_actions_per_step=5,
    )

    # Emit start screenshot
    await capture_and_emit("Starting agent — opening browser", "running")

    # ── Custom run loop: intercept each step ───────────────────────────────
    # browser-use's Agent.run() returns an AgentHistoryList.
    # We hook into it by overriding the internal _step method.

    original_step = agent._step if hasattr(agent, "_step") else None

    if original_step:
        async def hooked_step(*args, **kwargs):
            result = await original_step(*args, **kwargs)
            # After each internal step, grab the latest action description
            try:
                history    = agent.state.history if hasattr(agent, "state") else None
                last_action = ""
                if history and hasattr(history, "model_actions") and history.model_actions():
                    acts = history.model_actions()
                    if acts:
                        last_action = str(acts[-1])[:120]
            except Exception:
                last_action = "processing…"
            await capture_and_emit(last_action or "agent step in progress")
            return result

        agent._step = hooked_step

    # Run the agent
    history = await agent.run(max_steps=25)

    # Final screenshot
    await capture_and_emit("Task complete", "done")

    result = history.final_result() if hasattr(history, "final_result") else str(history)
    await browser.close()
    return result or "Task completed"
