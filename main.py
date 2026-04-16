"""
main.py  –  CLI runner. Panel must already be running on localhost:5000.

Usage:
    python main.py
    python main.py "reset password for alice@company.com"
    python main.py --headless "create user Jane Doe jane@company.com"
"""
import asyncio, sys, os
from dotenv import load_dotenv
load_dotenv()

missing = [k for k in ["GEMINI_API_KEY","GROQ_API_KEY"] if not os.getenv(k)]
if missing:
    print(f"[Error] Missing env vars: {', '.join(missing)}")
    sys.exit(1)

from agent.orchestrator import run_task

DEMOS = [
    "Reset the password for alice@company.com",
    "Create user John Doe john@company.com role employee",
    "Check if newuser@company.com exists, if not create them then assign pro license",
    "Disable bob@company.com",
    "Assign enterprise license to alice@company.com",
]

async def main():
    headless = "--headless" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if args:
        task = " ".join(args)
    else:
        print("\n" + "="*55 + "\n  Decawork IT Agent\n" + "="*55)
        for i, t in enumerate(DEMOS, 1): print(f"  {i}. {t}")
        print("  0. Custom\n" + "="*55)
        c = input("Choose: ").strip()
        task = DEMOS[int(c)-1] if c.isdigit() and 1<=int(c)<=len(DEMOS) else input("Task: ").strip()

    await run_task(task, headless=headless, run_id=0, sock=None)

if __name__ == "__main__":
    asyncio.run(main())
