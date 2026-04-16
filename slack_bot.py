"""
slack_bot.py  –  Trigger agent from Slack @mention.
See README for 10-min free setup.
"""
import asyncio, os, re, threading
from dotenv import load_dotenv; load_dotenv()
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from agent.orchestrator import run_task

app = App(token=os.environ["SLACK_BOT_TOKEN"])

@app.event("app_mention")
def handle_mention(event, say):
    task = re.sub(r"<@[A-Z0-9]+>", "", event.get("text","")).strip()
    if not task:
        say("Mention me with an IT request, e.g. `@ITBot reset password for user@company.com`"); return
    say(f"🤖 Running: *{task}*")
    def _run():
        result = asyncio.run(run_task(task, headless=True))
        app.client.chat_postMessage(channel=event["channel"],
            text=f"✅ Done!\n```{result[:400]}```")
    threading.Thread(target=_run, daemon=True).start()

if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
