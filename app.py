from __future__ import annotations

import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
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
DB_PATH = BASE_DIR / "disciple_live.db"
UPLOAD_DIR = BASE_DIR / "uploads"
ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "ppt", "pptx", "txt", "md"}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)
UPLOAD_DIR.mkdir(exist_ok=True)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_: object) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
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
        admin_exists = db.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        if not admin_exists:
            db.execute(
                """
                INSERT INTO users (name, email, password_hash, role, bio)
                VALUES (?, ?, ?, 'admin', ?)
                """,
                (
                    "Platform Admin",
                    "admin@disciple.live",
                    generate_password_hash("admin123"),
                    "Default admin account",
                ),
            )
        db.commit()


def current_user() -> sqlite3.Row | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


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
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        role = request.form["role"]
        bio = request.form.get("bio", "").strip()
        password = request.form["password"]

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
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid login credentials.", "danger")
            return redirect(url_for("login"))

        session["user_id"] = user["id"]
        flash("Welcome back!", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html", user=current_user())


@app.route("/logout")
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
            SELECT s.*, du.name AS disciplee_name, c.title AS module_title
            FROM sessions s
            JOIN users du ON s.disciplee_id = du.id
            LEFT JOIN content_modules c ON s.content_module_id = c.id
            WHERE s.discipler_id = ?
            ORDER BY s.created_at DESC
            """,
            (user["id"],),
        ).fetchall()
        return render_template(
            "disciplier_dashboard.html", user=user, incoming=incoming, slots=slots, sessions=sessions
        )

    disciplers = db.execute(
        "SELECT id, name, email, bio FROM users WHERE role = 'disciplier' ORDER BY name"
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

    modules = db.execute("SELECT * FROM content_modules ORDER BY created_at DESC").fetchall()
    sessions = db.execute(
        """
        SELECT s.*, du.name AS discipler_name, c.title AS module_title
        FROM sessions s
        JOIN users du ON s.discipler_id = du.id
        LEFT JOIN content_modules c ON s.content_module_id = c.id
        WHERE s.disciplee_id = ?
        ORDER BY s.created_at DESC
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
    discipler_id = int(request.form["discipler_id"])
    message = request.form.get("message", "").strip()

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
        (user["id"], discipler_id, message, datetime.utcnow().isoformat()),
    )
    db.commit()
    flash("Mentorship request sent.", "success")
    return redirect(url_for("dashboard"))


@app.route("/respond-request/<int:request_id>", methods=["POST"])
@login_required("disciplier")
def respond_request(request_id: int):
    decision = request.form["decision"]
    if decision not in {"accepted", "declined"}:
        flash("Invalid action.", "danger")
        return redirect(url_for("dashboard"))

    db = get_db()
    user = current_user()
    db.execute(
        "UPDATE mentor_requests SET status = ? WHERE id = ? AND discipler_id = ?",
        (decision, request_id, user["id"]),
    )
    db.commit()
    flash(f"Request {decision}.", "info")
    return redirect(url_for("dashboard"))


@app.route("/add-slot", methods=["POST"])
@login_required("disciplier")
def add_slot():
    slot_time = request.form["slot_time"]
    try:
        dt = datetime.fromisoformat(slot_time)
    except ValueError:
        flash("Invalid slot date/time.", "danger")
        return redirect(url_for("dashboard"))

    db = get_db()
    user = current_user()
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
    slot_id = int(request.form["slot_id"])
    module_raw = request.form.get("content_module_id")
    module_id = int(module_raw) if module_raw else None

    slot = db.execute("SELECT * FROM availability_slots WHERE id = ?", (slot_id,)).fetchone()
    if not slot or slot["is_booked"]:
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

    room = f"disciple-live-{secrets.token_hex(4)}"
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
            datetime.utcnow().isoformat(),
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
    title = request.form["title"].strip()
    description = request.form.get("description", "").strip()
    external_url = request.form.get("external_url", "").strip()
    uploaded = request.files.get("content_file")
    file_path = None

    if uploaded and uploaded.filename:
        filename = secure_filename(uploaded.filename)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ALLOWED_EXTENSIONS:
            flash("Unsupported file type.", "danger")
            return redirect(url_for("dashboard"))
        stored = f"{datetime.utcnow().timestamp()}_{filename}"
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
        (title, description, file_path, external_url, user["id"], datetime.utcnow().isoformat()),
    )
    db.commit()
    flash("Content module published.", "success")
    return redirect(url_for("dashboard"))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/session/<int:session_id>")
@login_required()
def session_room(session_id: int):
    db = get_db()
    user = current_user()
    sess = db.execute(
        """
        SELECT s.*, c.title AS module_title, c.description AS module_description, c.external_url, c.file_path
        FROM sessions s
        LEFT JOIN content_modules c ON c.id = s.content_module_id
        WHERE s.id = ?
        """,
        (session_id,),
    ).fetchone()

    if not sess:
        flash("Session not found.", "danger")
        return redirect(url_for("dashboard"))

    if user["id"] not in {sess["disciplee_id"], sess["disciplier_id"]} and user["role"] != "admin":
        flash("You cannot access this session.", "danger")
        return redirect(url_for("dashboard"))

    return render_template("session_room.html", user=user, sess=sess)


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
