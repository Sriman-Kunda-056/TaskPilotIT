"""
panel/app.py  –  IT Admin Panel (no WebSockets — screenshot polling only)
"""

import os, sys, secrets, string, sqlite3, threading, asyncio
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (Flask, render_template, request, redirect,
                   url_for, jsonify, session)
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "taskpilot-v2-secret-change-in-prod")

DB_PATH = Path(__file__).parent / "admin.db"
SCREENSHOTS_DIR = Path(__file__).parent / "static" / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

AGENT_KEY = os.getenv("PANEL_AGENT_KEY", "taskpilot-agent-key-change-me")


# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = get_db()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS admin_accounts (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            username           TEXT    UNIQUE NOT NULL,
            email              TEXT    NOT NULL,
            password_hash      TEXT    NOT NULL,
            reset_token        TEXT,
            reset_token_expiry TEXT,
            created_at         TEXT    DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL,
            email      TEXT    UNIQUE NOT NULL,
            role       TEXT    DEFAULT 'employee',
            active     INTEGER DEFAULT 1,
            password   TEXT    DEFAULT 'changeme123',
            created_at TEXT    DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS licenses (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            license_type  TEXT    NOT NULL,
            assigned_at   TEXT    DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            source     TEXT    DEFAULT 'panel',
            event      TEXT    NOT NULL,
            success    INTEGER DEFAULT 1,
            detail     TEXT,
            created_at TEXT    DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS agent_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task        TEXT    NOT NULL,
            status      TEXT    DEFAULT 'running',
            result      TEXT,
            created_at  TEXT    DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS agent_steps (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      INTEGER NOT NULL,
            step_num    INTEGER NOT NULL,
            description TEXT,
            status      TEXT    DEFAULT 'running',
            screenshot  TEXT,
            created_at  TEXT    DEFAULT CURRENT_TIMESTAMP
        );
    """)

    try:
        c.execute(
            "INSERT INTO admin_accounts(username,email,password_hash) VALUES(?,?,?)",
            ("admin", "admin@company.com", generate_password_hash("admin123")),
        )
    except sqlite3.IntegrityError:
        pass

    for name, email, role in [
        ("Alice Smith",    "alice@company.com",   "admin"),
        ("Bob Jones",      "bob@company.com",     "employee"),
        ("Carol Williams", "carol@company.com",   "employee"),
    ]:
        try:
            c.execute("INSERT INTO users(name,email,role) VALUES(?,?,?)", (name, email, role))
        except sqlite3.IntegrityError:
            pass

    c.commit()
    c.close()


def log_event(source, event, success, detail=""):
    c = get_db()
    c.execute("INSERT INTO logs(source,event,success,detail) VALUES(?,?,?,?)",
              (source, event, int(success), detail))
    c.commit()
    c.close()


init_db()


# ── Auth ──────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_id"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("admin_id"):
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        c = get_db()
        adm = c.execute("SELECT * FROM admin_accounts WHERE username=?", (username,)).fetchone()
        c.close()
        if adm and check_password_hash(adm["password_hash"], password):
            session["admin_id"]       = adm["id"]
            session["admin_username"] = adm["username"]
            log_event("panel", "admin_login", True, username)
            return redirect(url_for("dashboard"))
        log_event("panel", "admin_login_failed", False, username)
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    username = session.get("admin_username", "")
    session.clear()
    log_event("panel", "admin_logout", True, username)
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password_view():
    token_info = None
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        c = get_db()
        adm = c.execute("SELECT * FROM admin_accounts WHERE username=?", (username,)).fetchone()
        if adm:
            token  = secrets.token_urlsafe(32)
            expiry = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            c.execute(
                "UPDATE admin_accounts SET reset_token=?, reset_token_expiry=? WHERE id=?",
                (token, expiry, adm["id"]),
            )
            c.commit()
            token_info = {"token": token, "username": username}
        else:
            error = "Username not found."
        c.close()
    return render_template("forgot_password.html", token_info=token_info, error=error)


@app.route("/reset-admin-password/<token>", methods=["GET", "POST"])
def reset_admin_password(token):
    c   = get_db()
    adm = c.execute(
        "SELECT * FROM admin_accounts WHERE reset_token=? AND reset_token_expiry > CURRENT_TIMESTAMP",
        (token,),
    ).fetchone()
    if not adm:
        c.close()
        return render_template("reset_password.html", valid=False)

    success = False
    error   = None
    if request.method == "POST":
        pw      = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if len(pw) < 8:
            error = "Password must be at least 8 characters."
        elif pw != confirm:
            error = "Passwords do not match."
        else:
            c.execute(
                "UPDATE admin_accounts SET password_hash=?, reset_token=NULL, reset_token_expiry=NULL WHERE id=?",
                (generate_password_hash(pw), adm["id"]),
            )
            c.commit()
            log_event("panel", "admin_password_reset", True, adm["username"])
            success = True
    c.close()
    return render_template("reset_password.html", valid=True, success=success,
                           error=error, token=token)


@app.route("/agent-auth")
def agent_auth():
    if request.args.get("key") == AGENT_KEY:
        session["admin_id"]       = 0
        session["admin_username"] = "agent"
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    c = get_db()
    total   = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    active  = c.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
    recent  = c.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 6").fetchall()
    runs    = c.execute("SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT 5").fetchall()
    log_cnt = c.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    c.close()
    return render_template("index.html", total=total, active=active,
                           recent=recent, runs=runs, log_cnt=log_cnt)


@app.route("/users")
@login_required
def users():
    c = get_db()
    rows = c.execute("""
        SELECT u.*, l.license_type FROM users u
        LEFT JOIN licenses l ON u.id=l.user_id
        ORDER BY u.created_at DESC
    """).fetchall()
    c.close()
    pw_result = session.pop("_pw_result", None)
    banner    = session.pop("_banner", None)
    return render_template("users.html", users=rows, pw_result=pw_result, banner=banner)


@app.route("/users/create", methods=["POST"])
@login_required
def create_user():
    name     = request.form.get("name", "").strip()
    email    = request.form.get("email", "").strip().lower()
    role     = request.form.get("role", "employee")
    password = request.form.get("password", "").strip() or "changeme123"
    c = get_db()
    try:
        c.execute("INSERT INTO users(name,email,role,password) VALUES(?,?,?,?)",
                  (name, email, role, password))
        c.commit()
        session["_banner"] = {"type": "ok", "msg": f"User {name} ({email}) created."}
        log_event("panel", "user_created", True, f"{name} <{email}>")
    except sqlite3.IntegrityError:
        session["_banner"] = {"type": "err", "msg": f"{email} already exists."}
        log_event("panel", "user_created", False, f"duplicate: {email}")
    finally:
        c.close()
    return redirect(url_for("users"))


@app.route("/users/reset-password", methods=["POST"])
@login_required
def reset_password():
    email     = request.form.get("email", "").strip().lower()
    custom_pw = request.form.get("new_password", "").strip()
    generated = not bool(custom_pw)
    pw = custom_pw if custom_pw else \
        "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
    c = get_db()
    u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if u:
        c.execute("UPDATE users SET password=? WHERE email=?", (pw, email))
        c.commit()
        session["_pw_result"] = {"email": email, "password": pw, "generated": generated}
        log_event("panel", "password_reset", True, email)
    else:
        session["_banner"] = {"type": "err", "msg": f"{email} not found."}
        log_event("panel", "password_reset", False, f"not found: {email}")
    c.close()
    return redirect(url_for("users"))


@app.route("/users/toggle", methods=["POST"])
@login_required
def toggle_user():
    email = request.form.get("email", "").strip().lower()
    c = get_db()
    u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if u:
        ns = 0 if u["active"] else 1
        c.execute("UPDATE users SET active=? WHERE email=?", (ns, email))
        c.commit()
        state = "activated" if ns else "disabled"
        session["_banner"] = {"type": "ok", "msg": f"{email} {state}."}
        log_event("panel", "user_toggled", True, f"{email} → {state}")
    c.close()
    return redirect(url_for("users"))


@app.route("/users/delete", methods=["POST"])
@login_required
def delete_user():
    email = request.form.get("email", "").strip().lower()
    c = get_db()
    u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if u:
        c.execute("DELETE FROM licenses WHERE user_id=?", (u["id"],))
        c.execute("DELETE FROM users WHERE id=?", (u["id"],))
        c.commit()
        session["_banner"] = {"type": "ok", "msg": f"User {email} deleted."}
        log_event("panel", "user_deleted", True, email)
    else:
        session["_banner"] = {"type": "err", "msg": f"{email} not found."}
        log_event("panel", "user_deleted", False, f"not found: {email}")
    c.close()
    return redirect(url_for("users"))


@app.route("/licenses")
@login_required
def licenses():
    c = get_db()
    all_users = c.execute("SELECT * FROM users WHERE active=1 ORDER BY name").fetchall()
    assigned  = c.execute("""
        SELECT l.*,u.name,u.email FROM licenses l
        JOIN users u ON l.user_id=u.id ORDER BY l.assigned_at DESC
    """).fetchall()
    c.close()
    banner = session.pop("_banner", None)
    return render_template("licenses.html", users=all_users, assigned=assigned, banner=banner)


@app.route("/licenses/assign", methods=["POST"])
@login_required
def assign_license():
    email = request.form.get("email", "").strip().lower()
    ltype = request.form.get("license_type", "basic")
    c = get_db()
    u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if u:
        c.execute("DELETE FROM licenses WHERE user_id=?", (u["id"],))
        c.execute("INSERT INTO licenses(user_id,license_type) VALUES(?,?)", (u["id"], ltype))
        c.commit()
        session["_banner"] = {"type": "ok", "msg": f"{email} → {ltype} license assigned."}
        log_event("panel", "license_assigned", True, f"{email} → {ltype}")
    else:
        session["_banner"] = {"type": "err", "msg": f"{email} not found."}
        log_event("panel", "license_assigned", False, f"not found: {email}")
    c.close()
    return redirect(url_for("licenses"))


@app.route("/logs")
@login_required
def logs():
    source = request.args.get("source", "all")
    c = get_db()
    if source == "all":
        rows = c.execute("SELECT * FROM logs ORDER BY created_at DESC LIMIT 200").fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM logs WHERE source=? ORDER BY created_at DESC LIMIT 200",
            (source,),
        ).fetchall()
    c.close()
    return render_template("logs.html", logs=rows, source=source)


@app.route("/agent")
@login_required
def agent_page():
    c = get_db()
    runs = c.execute("SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT 20").fetchall()
    c.close()
    return render_template("agent.html", runs=runs)


# ── Agent API ─────────────────────────────────────────────────────────────────

@app.route("/api/agent/run", methods=["POST"])
@login_required
def api_agent_run():
    data = request.get_json(silent=True) or {}
    task = data.get("task", "").strip()
    if not task:
        return jsonify({"error": "No task provided"}), 400

    c = get_db()
    run_id = c.execute(
        "INSERT INTO agent_runs(task,status) VALUES(?,?)", (task, "running")
    ).lastrowid
    c.commit()
    c.close()
    log_event("agent", "task_started", True, task)

    try:
        from agent.orchestrator import run_task
    except Exception as e:
        c2 = get_db()
        c2.execute(
            "UPDATE agent_runs SET status='error',result=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (str(e), run_id),
        )
        c2.commit()
        c2.close()
        return jsonify({"error": str(e)}), 500

    def _run():
        try:
            result = asyncio.run(run_task(
                task,
                headless=True,
                run_id=run_id,
                db_path=str(DB_PATH),
                screenshots_dir=str(SCREENSHOTS_DIR),
            ))
            c2 = get_db()
            c2.execute(
                "UPDATE agent_runs SET status='completed',result=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
                (result[:500], run_id),
            )
            c2.commit()
            c2.close()
            log_event("agent", "task_completed", True, f"run#{run_id}")
        except Exception as e:
            c2 = get_db()
            c2.execute(
                "UPDATE agent_runs SET status='error',result=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(e), run_id),
            )
            c2.commit()
            c2.close()
            log_event("agent", "task_error", False, str(e))

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"run_id": run_id, "status": "started"})


@app.route("/api/agent/status/<int:run_id>")
@login_required
def api_agent_status(run_id):
    c = get_db()
    run = c.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
    steps = c.execute(
        "SELECT * FROM agent_steps WHERE run_id=? ORDER BY step_num, id",
        (run_id,),
    ).fetchall()
    c.close()
    if not run:
        return jsonify({"error": "Run not found"}), 404

    latest_screenshot = None
    for s in reversed(steps):
        if s["screenshot"]:
            latest_screenshot = s["screenshot"]
            break

    return jsonify({
        "run_id":             run_id,
        "status":             run["status"],
        "result":             run["result"],
        "steps":              [dict(s) for s in steps],
        "latest_screenshot":  latest_screenshot,
    })


@app.route("/api/agent/runs")
@login_required
def api_agent_runs():
    c = get_db()
    runs = c.execute("SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT 20").fetchall()
    c.close()
    return jsonify([dict(r) for r in runs])


@app.route("/api/users")
@login_required
def api_users():
    c = get_db()
    rows = c.execute("SELECT * FROM users").fetchall()
    c.close()
    return jsonify([dict(u) for u in rows])


@app.route("/api/users/<path:email>")
@login_required
def api_user(email):
    c = get_db()
    u = c.execute("SELECT * FROM users WHERE email=?", (email.lower(),)).fetchone()
    c.close()
    if u:
        return jsonify({"exists": True, "data": dict(u)})
    return jsonify({"exists": False})


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1"
    try:
        port = int(os.getenv("PORT", "5000"))
    except ValueError:
        port = 5000
    app.run(host="0.0.0.0", port=port, debug=debug_mode, use_reloader=False)
