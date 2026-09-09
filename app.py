from __future__ import annotations

import os
import secrets
import click
import re
import time
from urllib.parse import urlsplit
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    send_from_directory,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "instance")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "disciple_live.db"
UPLOAD_DIR = DATA_DIR / "uploads"
ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "ppt", "pptx", "txt", "md"}

app = Flask(__name__)
secret = os.environ.get("SECRET_KEY")
if not secret or len(secret) < 32:
    raise RuntimeError("Set SECRET_KEY to a random value of at least 32 characters.")
app.config.update(SECRET_KEY=secret, MAX_CONTENT_LENGTH=16 * 1024 * 1024,
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)
UPLOAD_DIR.mkdir(exist_ok=True)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
        g.db.execute("PRAGMA busy_timeout=5000")
    return g.db


@app.teardown_appcontext
def close_db(_: object) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    if (BASE_DIR / "disciple_live.db").exists() and not DB_PATH.exists():
        raise RuntimeError("Legacy database found. Copy it and uploads into DATA_DIR before initializing. See README.")
    schema = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role in ('admin', 'disciplier', 'disciplee')),
        bio TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS mentor_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        disciplee_id INTEGER NOT NULL,
        discipler_id INTEGER NOT NULL,
        message TEXT,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status in ('pending', 'accepted', 'declined')),
        created_at TEXT NOT NULL,
        FOREIGN KEY(disciplee_id) REFERENCES users(id),
        FOREIGN KEY(discipler_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS availability_slots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discipler_id INTEGER NOT NULL,
        slot_time TEXT NOT NULL,
        is_booked INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(discipler_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS content_modules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT,
        file_path TEXT,
        external_url TEXT,
        created_by INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(created_by) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mentor_request_id INTEGER NOT NULL,
        slot_id INTEGER NOT NULL,
        disciplee_id INTEGER NOT NULL,
        discipler_id INTEGER NOT NULL,
        content_module_id INTEGER,
        jitsi_room TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(mentor_request_id) REFERENCES mentor_requests(id),
        FOREIGN KEY(slot_id) REFERENCES availability_slots(id),
        FOREIGN KEY(disciplee_id) REFERENCES users(id),
        FOREIGN KEY(discipler_id) REFERENCES users(id),
        FOREIGN KEY(content_module_id) REFERENCES content_modules(id)
    );
    """

    with closing(sqlite3.connect(DB_PATH)) as db:
        db.executescript(schema)
        columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        if "timezone" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC'")
        if "auth_version" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN auth_version INTEGER NOT NULL DEFAULT 0")
        db.executescript("""
            CREATE INDEX IF NOT EXISTS idx_requests_disciple ON mentor_requests(disciplee_id, discipler_id);
            CREATE INDEX IF NOT EXISTS idx_requests_discipler ON mentor_requests(discipler_id, status);
            CREATE INDEX IF NOT EXISTS idx_slots_discipler ON availability_slots(discipler_id, slot_time);
            CREATE INDEX IF NOT EXISTS idx_sessions_disciple ON sessions(disciplee_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_discipler ON sessions(discipler_id);
        """)
        db.execute("PRAGMA optimize")
        db.execute("CREATE TABLE IF NOT EXISTS login_attempts (key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, started REAL NOT NULL)")
        # Refuse the legacy demo account rather than silently keeping a public password.
        legacy = db.execute("SELECT id, password_hash FROM users WHERE email='admin@disciple.live'").fetchone()
        if legacy and check_password_hash(legacy[1], "admin123"):
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(secrets.token_urlsafe(48)), legacy[0]))

        db.commit()


def current_user() -> sqlite3.Row | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user or session.get("auth_version") != user["auth_version"]:
        return None
    return user


def login_required(role: str | None = None):
    def decorator(view):
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                flash("Please log in first.", "warning")
                return redirect(url_for("login"))
            if role and user["role"] != role:
                flash("You do not have access to that page.", "danger")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)

        wrapped.__name__ = view.__name__
        return wrapped

    return decorator


@app.route("/")
def index():
    return render_template("index.html", user=current_user())


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        db = get_db()
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        role = request.form.get("role", "")
        bio = request.form.get("bio", "").strip()
        password = request.form.get("password", "")

        if not name or len(name) > 100 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254 or not 12 <= len(password) <= 128 or len(bio) > 2000:
            flash("Enter your name, a valid email, and a password of 12–128 characters. Keep your bio under 2,000 characters.", "warning")
            return redirect(url_for("register"))
        if role not in {"disciplier", "disciplee"}:
            flash("Invalid role selected.", "danger")
            return redirect(url_for("register"))

        try:
            db.execute(
                "INSERT INTO users (name, email, password_hash, role, bio) VALUES (?, ?, ?, ?, ?)",
                (name, email, generate_password_hash(password), role, bio),
            )
            db.commit()
            flash("Account created. You can now sign in.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("That email is already in use.", "danger")

    return render_template("register.html", user=current_user())


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        key = email[:254]
        db = get_db()
        now = time.time()
        db.execute("DELETE FROM login_attempts WHERE started < ?", (now - 900,))
        attempt = db.execute("SELECT * FROM login_attempts WHERE key=?", (key,)).fetchone()
        if attempt and attempt["attempts"] >= 10:
            db.commit()
            abort(429, "Too many sign-in attempts. Try again in 15 minutes.")
        db.execute("INSERT INTO login_attempts VALUES (?, 1, ?) ON CONFLICT(key) DO UPDATE SET attempts=attempts+1", (key, now))
        db.commit()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

        if len(password) > 128 or not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid login credentials.", "danger")
            return redirect(url_for("login"))

        db.execute("DELETE FROM login_attempts WHERE key=?", (key,))
        db.commit()
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        session["auth_version"] = user["auth_version"]
        flash("Welcome back!", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html", user=current_user())


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required()
def dashboard():
    user = current_user()
    db = get_db()

    if user["role"] == "admin":
        users = db.execute("SELECT * FROM users ORDER BY role, name").fetchall()
        modules = db.execute(
            """
            SELECT m.*, u.name AS creator_name
            FROM content_modules m
            JOIN users u ON m.created_by = u.id
            ORDER BY m.created_at DESC
            """
        ).fetchall()
        return render_template("admin_dashboard.html", user=user, users=users, modules=modules)

    if user["role"] == "disciplier":
        incoming = db.execute(
            """
            SELECT mr.*, du.name AS disciplee_name, du.email AS disciplee_email
            FROM mentor_requests mr
            JOIN users du ON mr.disciplee_id = du.id
            WHERE mr.discipler_id = ?
            ORDER BY mr.created_at DESC
            """,
            (user["id"],),
        ).fetchall()
        slots = db.execute(
            "SELECT * FROM availability_slots WHERE discipler_id = ? ORDER BY slot_time",
            (user["id"],),
        ).fetchall()
        sessions = db.execute(
            """
            SELECT s.*, a.slot_time, du.name AS disciplee_name, c.title AS module_title
            FROM sessions s
            JOIN availability_slots a ON a.id=s.slot_id
            JOIN users du ON s.disciplee_id = du.id
            LEFT JOIN content_modules c ON s.content_module_id = c.id
            WHERE s.discipler_id = ?
            ORDER BY a.slot_time
            """,
            (user["id"],),
        ).fetchall()
        return render_template(
            "disciplier_dashboard.html", user=user, incoming=incoming, slots=slots, sessions=sessions
        )

    disciplers = db.execute(
        "SELECT id, name, bio FROM users WHERE role = 'disciplier' ORDER BY name"
    ).fetchall()
    requests = db.execute(
        """
        SELECT mr.*, du.name AS discipler_name
        FROM mentor_requests mr
        JOIN users du ON mr.discipler_id = du.id
        WHERE mr.disciplee_id = ?
        ORDER BY mr.created_at DESC
        """,
        (user["id"],),
    ).fetchall()
    accepted_ids = [r["discipler_id"] for r in requests if r["status"] == "accepted"]

    slots = []
    if accepted_ids:
        placeholders = ",".join(["?"] * len(accepted_ids))
        slots = db.execute(
            f"""
            SELECT s.*, u.name AS discipler_name
            FROM availability_slots s
            JOIN users u ON s.discipler_id = u.id
            WHERE s.discipler_id IN ({placeholders}) AND s.is_booked = 0
            ORDER BY s.slot_time
            """,
            accepted_ids,
        ).fetchall()

    slots = [slot for slot in slots if slot["slot_time"] > utcnow()]
    modules = db.execute("SELECT * FROM content_modules ORDER BY created_at DESC").fetchall()
    sessions = db.execute(
        """
        SELECT s.*, a.slot_time, du.name AS discipler_name, c.title AS module_title
        FROM sessions s
        JOIN availability_slots a ON a.id=s.slot_id
        JOIN users du ON s.discipler_id = du.id
        LEFT JOIN content_modules c ON s.content_module_id = c.id
        WHERE s.disciplee_id = ?
        ORDER BY a.slot_time
        """,
        (user["id"],),
    ).fetchall()

    return render_template(
        "disciplee_dashboard.html",
        user=user,
        disciplers=disciplers,
        requests=requests,
        slots=slots,
        modules=modules,
        sessions=sessions,
    )


@app.route("/request-mentor", methods=["POST"])
@login_required("disciplee")
def request_mentor():
    user = current_user()
    db = get_db()
    discipler_id = positive_id("discipler_id")
    if not db.execute("SELECT id FROM users WHERE id=? AND role='disciplier'", (discipler_id,)).fetchone():
        abort(400, "Choose an available discipler.")
    message = request.form.get("message", "").strip()[:2000]

    db.execute("BEGIN IMMEDIATE")
    existing = db.execute(
        "SELECT id FROM mentor_requests WHERE disciplee_id = ? AND discipler_id = ?",
        (user["id"], discipler_id),
    ).fetchone()
    if existing:
        flash("You already sent a request to this discipler.", "warning")
        return redirect(url_for("dashboard"))

    db.execute(
        """
        INSERT INTO mentor_requests (disciplee_id, discipler_id, message, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
        """,
        (user["id"], discipler_id, message, utcnow()),
    )
    db.commit()
    flash("Mentorship request sent.", "success")
    return redirect(url_for("dashboard"))


@app.route("/respond-request/<int:request_id>", methods=["POST"])
@login_required("disciplier")
def respond_request(request_id: int):
    decision = request.form.get("decision", "")
    if decision not in {"accepted", "declined"}:
        flash("Invalid action.", "danger")
        return redirect(url_for("dashboard"))

    db = get_db()
    user = current_user()
    db.execute(
        "UPDATE mentor_requests SET status = ? WHERE id = ? AND discipler_id = ? AND status = 'pending'",
        (decision, request_id, user["id"]),
    )
    db.commit()
    flash(f"Request {decision}.", "info")
    return redirect(url_for("dashboard"))


@app.route("/add-slot", methods=["POST"])
@login_required("disciplier")
def add_slot():
    slot_time = request.form.get("slot_time", "")
    try:
        dt = datetime.fromisoformat(slot_time)
        zone = ZoneInfo(current_user()["timezone"])
        if dt.tzinfo is None:
            local = dt.replace(tzinfo=zone)
            if local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != dt:
                raise ValueError()
            if local.utcoffset() != local.replace(fold=1).utcoffset():
                raise ValueError()
            dt = local
        dt = dt.astimezone(timezone.utc)
        if dt <= datetime.now(timezone.utc):
            raise ValueError()
    except ValueError:
        flash("Choose a future time. Times skipped or repeated by daylight saving are unavailable.", "danger")
        return redirect(url_for("dashboard"))

    db = get_db()
    user = current_user()
    db.execute("BEGIN IMMEDIATE")
    if db.execute("SELECT id FROM availability_slots WHERE discipler_id=? AND abs(julianday(slot_time)-julianday(?))*1440 < 60", (user["id"], dt.isoformat())).fetchone():
        flash("Allow a full hour between meeting times.", "warning")
        return redirect(url_for("dashboard"))
    db.execute(
        "INSERT INTO availability_slots (discipler_id, slot_time, is_booked) VALUES (?, ?, 0)",
        (user["id"], dt.isoformat()),
    )
    db.commit()
    flash("Availability slot added.", "success")
    return redirect(url_for("dashboard"))


@app.route("/book-session", methods=["POST"])
@login_required("disciplee")
def book_session():
    db = get_db()
    user = current_user()
    slot_id = positive_id("slot_id")
    module_raw = request.form.get("content_module_id")
    module_id = positive_id("content_module_id") if module_raw else None
    if module_id and not db.execute("SELECT id FROM content_modules WHERE id=?", (module_id,)).fetchone():
        abort(400, "Choose an existing resource.")
    db.execute("BEGIN IMMEDIATE")

    slot = db.execute("SELECT * FROM availability_slots WHERE id = ?", (slot_id,)).fetchone()
    if not slot or slot["is_booked"] or slot["slot_time"] <= utcnow():
        flash("That slot is unavailable.", "danger")
        return redirect(url_for("dashboard"))

    mentor_req = db.execute(
        """
        SELECT * FROM mentor_requests
        WHERE disciplee_id = ? AND discipler_id = ? AND status = 'accepted'
        ORDER BY created_at DESC LIMIT 1
        """,
        (user["id"], slot["discipler_id"]),
    ).fetchone()
    if not mentor_req:
        flash("You can only book with accepted disciplers.", "danger")
        return redirect(url_for("dashboard"))

    if db.execute("SELECT s.id FROM sessions s JOIN availability_slots a ON a.id=s.slot_id WHERE s.disciplee_id=? AND abs(julianday(a.slot_time)-julianday(?))*1440 < 60", (user["id"], slot["slot_time"])).fetchone():
        flash("You already have a meeting during that hour.", "warning")
        return redirect(url_for("dashboard"))
    room = f"disciple-live-{secrets.token_hex(24)}"
    db.execute("UPDATE availability_slots SET is_booked = 1 WHERE id = ?", (slot_id,))
    db.execute(
        """
        INSERT INTO sessions (mentor_request_id, slot_id, disciplee_id, discipler_id, content_module_id, jitsi_room, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mentor_req["id"],
            slot_id,
            user["id"],
            slot["discipler_id"],
            module_id,
            room,
            utcnow(),
        ),
    )
    db.commit()

    flash("Session booked! Open your meeting room below.", "success")
    return redirect(url_for("dashboard"))


@app.route("/upload-content", methods=["POST"])
@login_required("admin")
def upload_content():
    db = get_db()
    user = current_user()
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    external_url = request.form.get("external_url", "").strip()
    if not title or len(title) > 200 or len(description) > 5000:
        abort(400, "Provide a title under 200 characters and a description under 5,000.")
    if external_url and not valid_url(external_url):
        abort(400, "Use a complete HTTPS resource link.")
    uploaded = request.files.get("content_file")
    file_path = None

    if uploaded and uploaded.filename:
        filename = secure_filename(uploaded.filename)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ALLOWED_EXTENSIONS:
            flash("Unsupported file type.", "danger")
            return redirect(url_for("dashboard"))
        stored = f"{secrets.token_hex(16)}_{filename}"
        uploaded.save(UPLOAD_DIR / stored)
        file_path = stored

    if not file_path and not external_url:
        flash("Upload a file or provide an external URL.", "danger")
        return redirect(url_for("dashboard"))

    db.execute(
        """
        INSERT INTO content_modules (title, description, file_path, external_url, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (title, description, file_path, external_url, user["id"], utcnow()),
    )
    db.commit()
    flash("Content module published.", "success")
    return redirect(url_for("dashboard"))


@app.route("/uploads/<path:filename>")
@login_required()
def uploaded_file(filename: str):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename, as_attachment=True)


@app.route("/session/<int:session_id>")
@login_required()
def session_room(session_id: int):
    db = get_db()
    user = current_user()
    sess = db.execute(
        """
        SELECT s.*, a.slot_time, c.title AS module_title, c.description AS module_description, c.external_url, c.file_path
        FROM sessions s
        JOIN availability_slots a ON a.id=s.slot_id
        LEFT JOIN content_modules c ON c.id = s.content_module_id
        WHERE s.id = ?
        """,
        (session_id,),
    ).fetchone()

    if not sess:
        flash("Session not found.", "danger")
        return redirect(url_for("dashboard"))

    if user["id"] not in {sess["disciplee_id"], sess["discipler_id"]} and user["role"] != "admin":
        flash("You cannot access this session.", "danger")
        return redirect(url_for("dashboard"))

    return render_template("session_room.html", user=user, sess=sess)



def utcnow():
    return datetime.now(timezone.utc).isoformat()


def positive_id(field):
    try:
        value = int(request.form.get(field, ""))
        if value < 1:
            raise ValueError()
        return value
    except ValueError:
        abort(400, "Choose a valid item.")


def valid_url(value):
    try:
        parsed = urlsplit(value)
        return len(value) <= 2000 and parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@app.context_processor
def helpers():
    return {"csrf_token": csrf_token}


@app.before_request
def protect_forms():
    if request.method == "POST":
        supplied = request.form.get("csrf_token", "")
        if not supplied or not secrets.compare_digest(supplied, session.get("csrf", "")):
            abort(400, "Your form expired. Reload the page and try again.")


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; frame-src https://meet.jit.si; img-src 'self' data:; form-action 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'"
    if app.config["SESSION_COOKIE_SECURE"]:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    return response


@app.template_filter("localtime")
def localtime(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    user = current_user()
    return dt.astimezone(ZoneInfo(user["timezone"] if user else "UTC")).strftime("%b %d, %Y · %I:%M %p %Z")


@app.route("/profile", methods=["GET", "POST"])
@login_required()
def profile():
    user = current_user()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        bio = request.form.get("bio", "").strip()
        zone = request.form.get("timezone", "UTC")
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            abort(400, "Choose a valid time zone, such as America/Los_Angeles.")
        if not name or len(name) > 100 or len(bio) > 2000:
            abort(400, "Provide a name under 100 characters and a bio under 2,000.")
        db = get_db()
        db.execute("UPDATE users SET name=?, bio=?, timezone=? WHERE id=?", (name, bio, zone, user["id"]))
        new_password = request.form.get("new_password", "")
        if new_password:
            if not check_password_hash(user["password_hash"], request.form.get("current_password", "")) or not 12 <= len(new_password) <= 128:
                db.rollback()
                abort(400, "Enter your current password and a new password of 12–128 characters.")
            db.execute("UPDATE users SET password_hash=?, auth_version=auth_version+1 WHERE id=?", (generate_password_hash(new_password), user["id"]))
        db.commit()
        if new_password:
            session["auth_version"] = user["auth_version"] + 1
        flash("Profile saved.", "success")
        return redirect(url_for("profile"))
    return render_template("profile.html", user=user)


@app.route("/resources")
@login_required()
def resources():
    query = request.args.get("q", "").strip()[:100]
    modules = get_db().execute("SELECT * FROM content_modules WHERE title LIKE ? OR description LIKE ? ORDER BY created_at DESC", ("%"+query+"%", "%"+query+"%")).fetchall()
    return render_template("resources.html", user=current_user(), modules=modules, query=query)


@app.route("/slots/<int:slot_id>/delete", methods=["POST"])
@login_required("disciplier")
def delete_slot(slot_id):
    db = get_db()
    result = db.execute("DELETE FROM availability_slots WHERE id=? AND discipler_id=? AND is_booked=0", (slot_id, current_user()["id"]))
    if not result.rowcount:
        abort(404, "This open time was not found.")
    db.commit()
    flash("Availability removed.", "success")
    return redirect(url_for("dashboard"))


@app.route("/sessions/<int:session_id>/cancel", methods=["POST"])
@login_required()
def cancel_session(session_id):
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    meeting = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not meeting or current_user()["id"] not in (meeting["disciplee_id"], meeting["discipler_id"]):
        abort(404, "Meeting not found.")
    db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    db.execute("UPDATE availability_slots SET is_booked=0 WHERE id=?", (meeting["slot_id"],))
    db.commit()
    flash("Meeting canceled. The time is available to book again.", "success")
    return redirect(url_for("dashboard"))


@app.route("/resources/<int:module_id>/delete", methods=["POST"])
@login_required("admin")
def delete_resource(module_id):
    db = get_db()
    module = db.execute("SELECT * FROM content_modules WHERE id=?", (module_id,)).fetchone()
    if not module:
        abort(404)
    db.execute("UPDATE sessions SET content_module_id=NULL WHERE content_module_id=?", (module_id,))
    db.execute("DELETE FROM content_modules WHERE id=?", (module_id,))
    db.commit()
    if module["file_path"]:
        (UPLOAD_DIR / Path(module["file_path"]).name).unlink(missing_ok=True)
    flash("Resource removed.", "success")
    return redirect(url_for("resources"))


@app.route("/health")
def health():
    get_db().execute("SELECT COUNT(*) FROM users").fetchone()
    return {"status": "ok"}


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
@app.errorhandler(429)
def error_page(error):
    return render_template("error.html", user=current_user(), error=error), error.code


@app.cli.command("init-db")
def init_command():
    init_db()
    click.echo("Database initialized. No default accounts were created.")


@app.cli.command("create-admin")
@click.option("--email", prompt=True)
@click.option("--name", prompt=True)
@click.password_option()
def create_admin(email, name, password):
    if not 12 <= len(password) <= 128 or not name.strip() or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise click.ClickException("Use at least 12 characters.")
    db = get_db()
    try:
        db.execute("INSERT INTO users(name,email,password_hash,role) VALUES (?,?,?,'admin')", (name, email.strip().lower(), generate_password_hash(password)))
        db.commit()
    except sqlite3.IntegrityError:
        raise click.ClickException("This email already exists.")
    click.echo("Administrator created.")


@app.cli.command("reset-password")
@click.option("--email", prompt=True)
@click.password_option()
def reset_password(email, password):
    if not 12 <= len(password) <= 128:
        raise click.ClickException("Use 12–128 characters.")
    db = get_db()
    result = db.execute("UPDATE users SET password_hash=?, auth_version=auth_version+1 WHERE email=?", (generate_password_hash(password), email.strip().lower()))
    if not result.rowcount:
        raise click.ClickException("Account not found.")
    db.commit()
    click.echo("Password changed. Verify the member's identity before using this command.")


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000)
