"""
agent/browser_agent.py
Playwright + Gemini Vision — structured plan execution.

Direct steps (navigate/fill/select/click) run without Gemini.
Vision steps call Gemini only when the screen must be inspected.
"""

import asyncio
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

PANEL_URL = os.getenv("PANEL_URL", "http://localhost:5000")

# Used for all vision steps (also serves as fallback when Groq plan is unavailable)
_SYSTEM = f"""You are a browser automation agent controlling a real Chromium browser.
Complete IT admin tasks on the panel at {PANEL_URL}.

Panel pages:
  /           Dashboard
  /users      Create User · Reset Password · Toggle Status · Delete User (red Delete button per row)
  /licenses   Assign License (basic / pro / enterprise)
  /logs       Activity log

You receive the task, current URL, step number, action history, and a screenshot.
Respond with EXACTLY ONE action — no explanation, no markdown, no extra text.

Action formats:
  navigate|http://full-url
  click|X|Y
  fill|css-selector|text to fill
  type|text to type here
  press|Tab
  press|Enter
  select|css-selector|option-value
  done|summary of what was accomplished

Rules:
- Always navigate to the correct page first.
- Prefer fill|selector|value over click+type when you know the CSS selector for an input.
- Click an input field before typing into it when you don't know the selector.
- After filling all required fields, click the submit button.
- To delete a user: go to /users, find the user row in the All Users table, click the red Delete button.
- When the task is fully complete respond with done|...
- If stuck after repeated tries respond with done|FAILED: reason
"""


def _prompt(task: str, url: str, step: int, max_steps: int, history: list[str]) -> str:
    hist = " → ".join(history[-6:]) if history else "none"
    return (
        f"Task: {task}\n"
        f"URL: {url}\n"
        f"Step: {step}/{max_steps}   History: {hist}\n\n"
        "Look at the screenshot and output the next single action."
    )


def _write_step(db_path: str, run_id: int, step_num: int,
                description: str, status: str, screenshot: Optional[str] = None):
    if not db_path:
        return
    c = sqlite3.connect(db_path)
    c.execute(
        "INSERT INTO agent_steps(run_id,step_num,description,status,screenshot) VALUES(?,?,?,?,?)",
        (run_id, step_num, description, status, screenshot),
    )
    c.commit()
    c.close()


async def _execute(page, action: str) -> bool:
    """Execute one action string. Returns True when action is 'done'."""
    line = action.strip()

    if line.startswith("navigate|"):
        url = line.split("|", 1)[1].strip()
        await page.goto(url, timeout=20_000, wait_until="domcontentloaded")
        await page.wait_for_timeout(700)

    elif line.startswith("click|"):
        parts = line.split("|")
        await page.mouse.click(int(parts[1]), int(parts[2]))
        await page.wait_for_timeout(350)

    elif line.startswith("fill|"):
        parts = line.split("|", 2)
        await page.fill(parts[1].strip(), parts[2])
        await page.wait_for_timeout(200)

    elif line.startswith("type|"):
        text = line.split("|", 1)[1]
        await page.keyboard.type(text, delay=25)
        await page.wait_for_timeout(200)

    elif line.startswith("press|"):
        key = line.split("|", 1)[1].strip()
        await page.keyboard.press(key)
        await page.wait_for_timeout(300)

    elif line.startswith("select|"):
        parts = line.split("|", 2)
        await page.select_option(parts[1].strip(), value=parts[2].strip())
        await page.wait_for_timeout(250)

    elif line.startswith("done|"):
        return True

    return False


async def run_browser_agent(
    task: str,
    run_id: int,
    db_path: str,
    screenshots_dir: str,
    headless: bool = True,
) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY — add it to .env")

    model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-lite")

    try:
        from google import genai
        from google.genai import types as gt
    except ImportError:
        raise RuntimeError("google-genai not found. Run: pip install google-genai")

    client = genai.Client(api_key=api_key)
    shots_dir = Path(screenshots_dir) if screenshots_dir else Path("screenshots")
    shots_dir.mkdir(parents=True, exist_ok=True)

    step_num = [0]

    def save_step(desc: str, status: str, screenshot: Optional[str] = None):
        step_num[0] += 1
        _write_step(db_path, run_id, step_num[0], desc, status, screenshot)

    # Parse JSON plan from task_planner, or fall back to a single vision step
    try:
        plan = json.loads(task)
        if not isinstance(plan, list):
            raise ValueError("not a list")
    except Exception:
        plan = [{"action": "vision", "instruction": task}]

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--window-size=1280,900", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = await browser.new_page(viewport={"width": 1280, "height": 900})

        # Auto-accept JS confirm/alert dialogs so form submissions aren't blocked
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))

        save_step("Browser started — connecting to panel", "running")

        admin_user = os.getenv("PANEL_ADMIN_USER", "admin")
        admin_pass = os.getenv("PANEL_ADMIN_PASSWORD", "admin123")

        try:
            await page.goto(PANEL_URL, timeout=15_000, wait_until="domcontentloaded")
            await page.wait_for_timeout(400)
        except Exception as e:
            raise RuntimeError(
                f"Cannot reach panel at {PANEL_URL}. "
                f"Make sure the panel is running.\nDetail: {e}"
            )

        if "/login" in page.url:
            save_step("Login page detected — signing in", "running")
            await page.fill('input[name="username"]', admin_user)
            await page.fill('input[name="password"]', admin_pass)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(800)
            if "/login" in page.url:
                raise RuntimeError(
                    "Panel login failed. Check PANEL_ADMIN_USER / PANEL_ADMIN_PASSWORD in .env"
                )

        n_vision = sum(1 for s in plan if s.get("action") == "vision")
        n_direct = len(plan) - n_vision
        save_step(
            f"Signed in. Plan: {n_direct} direct + {n_vision} vision step(s)",
            "running",
        )

        # Gemini call budget: more steps when the whole task is one vision step (Groq fallback)
        vision_budget = 25 if (len(plan) == 1 and n_vision == 1) else 15
        history: list[str] = []

        try:
            for plan_item in plan:
                act = plan_item.get("action", "vision")

                # ── Direct steps: no Gemini call ──────────────────────────────
                # On failure, act is set to "vision" so the block below takes over.

                if act == "navigate":
                    url = plan_item.get("url", "/")
                    full_url = PANEL_URL + url if url.startswith("/") else url
                    save_step(f"Navigate → {url}", "running")
                    try:
                        await page.goto(full_url, timeout=20_000, wait_until="domcontentloaded")
                        await page.wait_for_timeout(600)
                    except Exception as e:
                        save_step(f"Navigate failed ({e})", "running")

                elif act == "fill":
                    sel, val = plan_item["selector"], plan_item["value"]
                    save_step(f"Fill [{sel}] ← '{val[:50]}'", "running")
                    try:
                        await page.fill(sel, val)
                        await page.wait_for_timeout(200)
                    except Exception as e:
                        save_step(f"Fill failed ({e}) — falling back to vision", "running")
                        plan_item = {"action": "vision", "instruction": f"Fill the field for '{val}' and continue the task"}
                        act = "vision"

                elif act == "select":
                    sel, val = plan_item["selector"], plan_item["value"]
                    save_step(f"Select '{val}' in [{sel}]", "running")
                    try:
                        await page.select_option(sel, value=val)
                        await page.wait_for_timeout(200)
                    except Exception as e:
                        save_step(f"Select failed ({e}) — falling back to vision", "running")
                        plan_item = {"action": "vision", "instruction": f"Select '{val}' in the appropriate dropdown and continue the task"}
                        act = "vision"

                elif act == "click":
                    sel = plan_item["selector"]
                    save_step(f"Click [{sel}]", "running")
                    try:
                        await page.click(sel)
                        await page.wait_for_timeout(500)
                    except Exception as e:
                        save_step(f"Click failed ({e}) — falling back to vision", "running")
                        plan_item = {"action": "vision", "instruction": "Click the submit/action button and continue the task"}
                        act = "vision"

                elif act == "delete-user":
                    email = plan_item.get("email", "").strip().lower()
                    save_step(f"Delete user {email}", "running")
                    try:
                        # Navigate to /users if not already there
                        if "/users" not in page.url:
                            await page.goto(
                                PANEL_URL + "/users", timeout=20_000,
                                wait_until="domcontentloaded",
                            )
                            await page.wait_for_timeout(600)
                        # Submit the hidden delete form for this email via JS
                        email_safe = email.replace("'", "\\'")
                        deleted = await page.evaluate(f"""
                            (() => {{
                                const inputs = document.querySelectorAll(
                                    'form[action="/users/delete"] input[name="email"]'
                                );
                                for (const inp of inputs) {{
                                    if (inp.value.toLowerCase() === '{email_safe}') {{
                                        inp.closest('form').submit();
                                        return true;
                                    }}
                                }}
                                return false;
                            }})()
                        """)
                        await page.wait_for_timeout(800)
                        if not deleted:
                            raise RuntimeError(f"User '{email}' not found in the table")
                        save_step(f"User {email} deleted successfully", "running")
                    except Exception as e:
                        save_step(f"Direct delete failed ({e}) — falling back to vision", "running")
                        plan_item = {
                            "action": "vision",
                            "instruction": f"Find {email} in the All Users table and click the Delete button in their row. Confirm any dialog.",
                        }
                        act = "vision"

                # ── Vision step: Gemini loop until done ───────────────────────
                # Uses `if` not `elif` so it also catches direct-step fallbacks above.

                if act == "vision":
                    instruction = plan_item.get("instruction", "Complete the current task")
                    save_step(f"[Vision] {instruction[:80]}", "running")

                    for v_step in range(1, vision_budget + 1):
                        try:
                            shot = await page.screenshot(full_page=False)
                        except Exception:
                            await asyncio.sleep(0.5)
                            shot = await page.screenshot(full_page=False)

                        fname = f"run_{run_id}_step_{step_num[0]}.png"
                        (shots_dir / fname).write_bytes(shot)

                        # Gemini call with rate-limit retry
                        action_line = None
                        for attempt in range(1, 4):
                            try:
                                resp = client.models.generate_content(
                                    model=model_name,
                                    contents=[
                                        gt.Part.from_bytes(data=shot, mime_type="image/png"),
                                        gt.Part.from_text(text=_prompt(
                                            instruction, page.url,
                                            v_step, vision_budget, history,
                                        )),
                                    ],
                                    config=gt.GenerateContentConfig(
                                        system_instruction=_SYSTEM,
                                        temperature=0.1,
                                    ),
                                )
                                raw = resp.text.strip()
                                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                                raw = re.sub(r"\n?```$", "", raw).strip()
                                for candidate in raw.splitlines():
                                    if candidate.strip():
                                        action_line = candidate.strip()
                                        break
                                break
                            except Exception as e:
                                err = str(e)
                                if ("429" in err or "quota" in err.lower()) and attempt < 3:
                                    wait = 15 * attempt
                                    save_step(
                                        f"Rate limit — waiting {wait}s (attempt {attempt}/3)",
                                        "running", fname,
                                    )
                                    time.sleep(wait)
                                elif "429" in err or "quota" in err.lower():
                                    raise RuntimeError(
                                        f"Gemini quota exceeded for '{model_name}'. "
                                        "Check limits at aistudio.google.com.\n"
                                        f"Detail: {err[:300]}"
                                    )
                                else:
                                    raise RuntimeError(f"Gemini API error: {err[:300]}")

                        vision_budget -= 1
                        is_done = action_line.startswith("done|")
                        save_step(action_line[:200], "done" if is_done else "running", fname)
                        history.append(action_line[:70])

                        if is_done:
                            break

                        try:
                            done = await _execute(page, action_line)
                            if done:
                                break
                        except Exception as exec_err:
                            save_step(f"Action failed ({exec_err}) — retrying", "running", fname)
                            await page.wait_for_timeout(600)

                        # Free-tier rate limit: 15 RPM → 4 s between Gemini calls
                        time.sleep(4)

            save_step("All steps completed", "done")
            return "Task completed successfully"

        except Exception as exc:
            save_step(f"Agent stopped: {exc}", "error")
            raise

        finally:
            await browser.close()
