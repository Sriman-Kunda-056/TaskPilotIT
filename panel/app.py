"""
panel/app.py  –  IT Admin Panel + Agent WebSocket hub
"""

import os, sys, secrets, string, sqlite3, base64, json
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv

app = Flask(__name__)
app.secret_key = "decawork-v2-secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

DB_PATH = os.path.join(os.path.dirname(__file__), "admin.db")
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Ensure API keys are available when launching the panel directly
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))


# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = get_db()
    c.executescript("""
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
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task       TEXT    NOT NULL,
            status     TEXT    DEFAULT 'running',
            result     TEXT,
            steps      INTEGER DEFAULT 0,
            created_at TEXT    DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT
        );
    """)
    for name, email, role in [
        ("Alice Smith",    "alice@company.com",   "admin"),
        ("Bob Jones",      "bob@company.com",     "employee"),
        ("Carol Williams", "carol@company.com",   "employee"),
    ]:
        try:
            c.execute("INSERT INTO users (name,email,role) VALUES(?,?,?)", (name,email,role))
        except sqlite3.IntegrityError:
            pass
    c.commit(); c.close()


def log_event(source, event, success, detail=""):
    c = get_db()
    c.execute("INSERT INTO logs(source,event,success,detail) VALUES(?,?,?,?)",
              (source, event, int(success), detail))
    c.commit(); c.close()


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
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
def users():
    c = get_db()
    rows = c.execute("""
        SELECT u.*, l.license_type FROM users u
        LEFT JOIN licenses l ON u.id=l.user_id
        ORDER BY u.created_at DESC
    """).fetchall()
    c.close()
    return render_template("users.html", users=rows)


@app.route("/users/create", methods=["POST"])
def create_user():
    name  = request.form.get("name","").strip()
    email = request.form.get("email","").strip().lower()
    role  = request.form.get("role","employee")
    c = get_db()
    try:
        c.execute("INSERT INTO users(name,email,role) VALUES(?,?,?)", (name,email,role))
        c.commit()
        ev = {"event":"user_created","success":True,"data":{"name":name,"email":email,"role":role}}
        log_event("panel","user_created",True,f"{name} <{email}>")
    except sqlite3.IntegrityError:
        ev = {"event":"user_created","success":False,"error":f"{email} already exists"}
        log_event("panel","user_created",False,f"duplicate: {email}")
    finally: c.close()
    socketio.emit("action_result", ev)
    return redirect(url_for("users"))


@app.route("/users/reset-password", methods=["POST"])
def reset_password():
    email      = request.form.get("email","").strip().lower()
    custom_pw  = request.form.get("new_password","").strip()
    generated  = not bool(custom_pw)
    pw = custom_pw if custom_pw else \
         "".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(12))
    c = get_db()
    u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if u:
        c.execute("UPDATE users SET password=? WHERE email=?", (pw,email))
        c.commit()
        ev = {"event":"password_reset","success":True,
              "data":{"email":email,"new_password":pw,"generated":generated}}
        log_event("panel","password_reset",True,email)
    else:
        ev = {"event":"password_reset","success":False,"error":f"{email} not found"}
        log_event("panel","password_reset",False,f"not found: {email}")
    c.close()
    socketio.emit("action_result", ev)
    return redirect(url_for("users"))


@app.route("/users/toggle", methods=["POST"])
def toggle_user():
    email = request.form.get("email","").strip().lower()
    c = get_db()
    u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if u:
        ns = 0 if u["active"] else 1
        c.execute("UPDATE users SET active=? WHERE email=?", (ns,email))
        c.commit()
        ev = {"event":"user_toggled","success":True,"data":{"email":email,"active":bool(ns)}}
        log_event("panel","user_toggled",True,f"{email} → {'active' if ns else 'disabled'}")
        socketio.emit("action_result", ev)
    c.close()
    return redirect(url_for("users"))


@app.route("/licenses")
def licenses():
    c = get_db()
    all_users = c.execute("SELECT * FROM users WHERE active=1 ORDER BY name").fetchall()
    assigned  = c.execute("""
        SELECT l.*,u.name,u.email FROM licenses l
        JOIN users u ON l.user_id=u.id ORDER BY l.assigned_at DESC
    """).fetchall()
    c.close()
    return render_template("licenses.html", users=all_users, assigned=assigned)


@app.route("/licenses/assign", methods=["POST"])
def assign_license():
    email = request.form.get("email","").strip().lower()
    ltype = request.form.get("license_type","basic")
    c = get_db()
    u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if u:
        c.execute("DELETE FROM licenses WHERE user_id=?", (u["id"],))
        c.execute("INSERT INTO licenses(user_id,license_type) VALUES(?,?)", (u["id"],ltype))
        c.commit()
        ev = {"event":"license_assigned","success":True,"data":{"email":email,"license_type":ltype}}
        log_event("panel","license_assigned",True,f"{email} → {ltype}")
    else:
        ev = {"event":"license_assigned","success":False,"error":f"{email} not found"}
        log_event("panel","license_assigned",False,f"not found: {email}")
    c.close()
    socketio.emit("action_result", ev)
    return redirect(url_for("licenses"))


@app.route("/logs")
def logs():
    source = request.args.get("source","all")
    c = get_db()
    if source == "all":
        rows = c.execute("SELECT * FROM logs ORDER BY created_at DESC LIMIT 200").fetchall()
    else:
        rows = c.execute("SELECT * FROM logs WHERE source=? ORDER BY created_at DESC LIMIT 200",
                         (source,)).fetchall()
    c.close()
    return render_template("logs.html", logs=rows, source=source)


@app.route("/agent")
def agent_page():
    c = get_db()
    runs = c.execute("SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT 20").fetchall()
    c.close()
    return render_template("agent.html", runs=runs)


# ── Agent API ──────────────────────────────────────────────────────────────────

@app.route("/api/agent/run", methods=["POST"])
def api_agent_run():
    """Start an agent task from the web UI."""
    data = request.get_json(silent=True) or {}
    task = data.get("task","").strip()
    if not task:
        return jsonify({"error":"No task provided"}), 400

    c = get_db()
    run_id = c.execute(
        "INSERT INTO agent_runs(task,status) VALUES(?,?)", (task,"running")
    ).lastrowid
    c.commit(); c.close()

    # Emit start event to agent page
    socketio.emit("agent_status", {"status":"running","run_id":run_id,"task":task})
    log_event("agent","task_started",True,task)

    # Import here to avoid circular issues at module level
    import threading, asyncio
    try:
        from agent.orchestrator import run_task
    except Exception as e:
        c2 = get_db()
        c2.execute(
            """UPDATE agent_runs SET status='error',result=?,
                          finished_at=CURRENT_TIMESTAMP WHERE id=?""",
            (f"Failed to import orchestrator: {e}", run_id),
        )
        c2.commit(); c2.close()
        socketio.emit("agent_status", {"status":"error","run_id":run_id,"error":str(e)})
        log_event("agent","task_error",False,f"import failure: {e}")
        return jsonify({"error":f"Failed to import orchestrator: {e}"}), 500

    def _run():
        try:
            result = asyncio.run(run_task(task, headless=True,
                                          run_id=run_id, sock=socketio))
            c2 = get_db()
            c2.execute("""UPDATE agent_runs SET status='completed',result=?,
                          finished_at=CURRENT_TIMESTAMP WHERE id=?""", (result[:500],run_id))
            c2.commit(); c2.close()
            socketio.emit("agent_status", {"status":"completed","run_id":run_id,"result":result})
            log_event("agent","task_completed",True,f"run#{run_id}")
        except Exception as e:
            c2 = get_db()
            c2.execute("""UPDATE agent_runs SET status='error',result=?,
                          finished_at=CURRENT_TIMESTAMP WHERE id=?""", (str(e),run_id))
            c2.commit(); c2.close()
            socketio.emit("agent_status", {"status":"error","run_id":run_id,"error":str(e)})
            log_event("agent","task_error",False,str(e))

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"run_id":run_id,"status":"started"})


@app.route("/api/agent/runs")
def api_agent_runs():
    c = get_db()
    runs = c.execute("SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT 20").fetchall()
    c.close()
    return jsonify([dict(r) for r in runs])


@app.route("/api/users")
def api_users():
    c = get_db()
    users = c.execute("SELECT * FROM users").fetchall()
    c.close()
    return jsonify([dict(u) for u in users])


@app.route("/api/users/<path:email>")
def api_user(email):
    c = get_db()
    u = c.execute("SELECT * FROM users WHERE email=?", (email.lower(),)).fetchone()
    c.close()
    if u: return jsonify({"exists":True,"data":dict(u)})
    return jsonify({"exists":False})


# ── WebSocket: agent pushes screenshot + step updates ─────────────────────────

@socketio.on("agent_screenshot")
def handle_screenshot(data):
    # Re-broadcast to all clients watching the agent page
    emit("agent_screenshot", data, broadcast=True)


@socketio.on("agent_step")
def handle_step(data):
    emit("agent_step", data, broadcast=True)
    # Persist step count
    run_id = data.get("run_id")
    if run_id:
        c = get_db()
        c.execute("UPDATE agent_runs SET steps=steps+1 WHERE id=?", (run_id,))
        c.commit(); c.close()


if __name__ == "__main__":
    init_db()
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, allow_unsafe_werkzeug=True)
