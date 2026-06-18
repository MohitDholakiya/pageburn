"""
Reading Club — a personal book-reading journal with streaks and rewards.

Stack: stdlib only (Python 3.14). No pip, no FastAPI, no npm.
  - wsgiref.simple_server for the HTTP server
  - sqlite3 for storage
  - hashlib.pbkdf2_hmac for password hashing
  - http.cookies + DB-backed sessions for auth
  - Python-side HTML rendering wrapped by base.html

Run:
  python3 app/server.py
Then open http://127.0.0.1:8765/

Default bootstrap user: mohit / reading  (change on first login)
"""
from __future__ import annotations

import hashlib
import hmac
import http.cookies
import html
import os
import re
import secrets
import sqlite3
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from wsgiref.simple_server import make_server, WSGIRequestHandler

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "pageburn.db"
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
HOST = os.environ.get("PAGEBURN_HOST", "0.0.0.0")
PORT = int(os.environ.get("PAGEBURN_PORT", "8765"))

SESSION_COOKIE = "rc_sid"

# Reward tuning (defaults confirmed by user)
POINTS_PER_ENTRY = 10
POINTS_PER_STREAK_DAY = 2
POINTS_FIRST_ENTRY_ON_BOOK = 25
POINTS_FINISH_BOOK = 100
STREAK_FREEZE_PERIOD_DAYS = 7  # 1 freeze earned per 7-day streak

# Open Library — free cover images, no API key needed.
# Cover URLs follow: https://covers.openlibrary.org/b/isbn/{isbn}-M.jpg
# We search by title+author to get the work key, then pick the best cover.
OPEN_LIBRARY_SEARCH = "https://openlibrary.org/search.json"
OPEN_LIBRARY_COVER_BASE = "https://covers.openlibrary.org/b/id/"


def fetch_cover_url(title: str, author: str | None) -> str | None:
    """Query Open Library for a book cover by title and optional author.
    Returns a cover image URL or None if nothing is found.
    Must NOT be called inside a WSGI request handler — use _fetch_cover_async instead."""
    try:
        import urllib.request, json, time
        query = title
        if author:
            query += f" {author}"
        url = f"{OPEN_LIBRARY_SEARCH}?q={urllib.parse.quote(query)}&fields=key,title,author_name,cover_i&limit=5&timeout=8"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Pageburn/1.0 (personal reading journal)"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        docs = data.get("docs", [])
        if not docs:
            return None
        # Pick the doc with the highest cover_i (usually the most relevant edition)
        best = max(docs, key=lambda d: d.get("cover_i") or 0)
        cover_id = best.get("cover_i")
        if cover_id:
            # Try M (medium, ~300px wide) first, fall back to S (small)
            url_m = f"{OPEN_LIBRARY_COVER_BASE}{cover_id}-M.jpg"
            req2 = urllib.request.Request(url_m, headers={"User-Agent": "Mozilla/5.0"})
            try:
                with urllib.request.urlopen(req2, timeout=8) as r2:
                    if r2.status == 200 and int(r2.headers.get("Content-Length", 0)) > 5000:
                        time.sleep(0.3)  # be polite to the free OL CDN
                        return url_m
            except Exception:
                pass
            # Fall back to S size
            url_s = f"{OPEN_LIBRARY_COVER_BASE}{cover_id}-S.jpg"
            return url_s
        return None
    except Exception:
        return None


def _fetch_cover_async(book_id: int, title: str, author: str | None) -> None:
    """Fetch cover in a background thread and update the DB. Safe to call from a request handler."""
    import threading
    def _bg():
        url = fetch_cover_url(title, author)
        if url:
            try:
                with get_db() as conn:
                    conn.execute("UPDATE books SET cover_url = ? WHERE id = ?", (url, book_id))
            except Exception:
                pass
    t = threading.Thread(target=_bg, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    author TEXT,
    cover_url TEXT,
    status TEXT NOT NULL DEFAULT 'reading',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    book_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    points_awarded INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(book_id) REFERENCES books(id)
);

CREATE TABLE IF NOT EXISTS state (
    user_id INTEGER PRIMARY KEY,
    current_streak INTEGER NOT NULL DEFAULT 0,
    longest_streak INTEGER NOT NULL DEFAULT 0,
    last_entry_date TEXT,
    total_points INTEGER NOT NULL DEFAULT 0,
    streak_freezes_earned INTEGER NOT NULL DEFAULT 0,
    streak_freezes_used INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_entries_user_date ON entries(user_id, entry_date);
CREATE INDEX IF NOT EXISTS idx_entries_book ON entries(book_id);
CREATE INDEX IF NOT EXISTS idx_books_user ON books(user_id);
"""


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(SCHEMA)
        # Ensure work_key column exists on upgrades
        try:
            conn.execute("ALTER TABLE books ADD COLUMN work_key TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        cur = conn.execute("SELECT COUNT(*) AS n FROM users")
        if cur.fetchone()["n"] == 0:
            username = "mohit"
            password = "reading"
            salt = secrets.token_hex(16)
            ph = hash_password(password, salt)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO users(username, password_hash, password_salt, created_at) VALUES (?,?,?,?)",
                (username, ph, salt, now),
            )
            user_id = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
            conn.execute("INSERT INTO state(user_id) VALUES (?)", (user_id,))
            today = date.today().isoformat()
            # Atomic Habits cover from Open Library (hardcoded — network may not allow live fetch in all envs)
            # Atomic Habits: cover + work_key from Open Library
            conn.execute(
                "INSERT INTO books(user_id, title, author, cover_url, work_key, status, started_at) VALUES (?,?,?,?,?,?,?)",
                (user_id, "Atomic Habits", "James Clear",
                 "https://covers.openlibrary.org/b/id/8739161-M.jpg",
                 "/works/OL1976652W", "reading", today),
            )
            print(f"[pageburn] created user '{username}' with password '{password}'")


# ---------------------------------------------------------------------------
# Auth / session
# ---------------------------------------------------------------------------

def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_password(password, salt), expected_hash)


def make_session_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=30)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (token, user_id, now.isoformat(), expires.isoformat()),
        )
    return token


def lookup_session(token: str) -> dict | None:
    if not token:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT s.token, s.user_id, s.expires_at, u.username FROM sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        return None
    return {"user_id": row["user_id"], "username": row["username"]}


def destroy_session(token: str) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# ---------------------------------------------------------------------------
# Streak + reward engine
# ---------------------------------------------------------------------------

def compute_entry_points(conn: sqlite3.Connection, user_id: int, book_id: int) -> tuple[int, dict]:
    s = conn.execute("SELECT * FROM state WHERE user_id = ?", (user_id,)).fetchone()
    cur = (s["current_streak"] if s else 0) or 0
    base = POINTS_PER_ENTRY
    streak_bonus = cur * POINTS_PER_STREAK_DAY
    first = conn.execute(
        "SELECT COUNT(*) AS n FROM entries WHERE user_id = ? AND book_id = ?",
        (user_id, book_id),
    ).fetchone()["n"] == 0
    first_bonus = POINTS_FIRST_ENTRY_ON_BOOK if first else 0
    total = base + streak_bonus + first_bonus
    return total, {"base": base, "streak_bonus": streak_bonus, "first_bonus": first_bonus, "total": total}


def _award_streak_freeze_if_due(conn: sqlite3.Connection, user_id: int) -> None:
    s = conn.execute("SELECT * FROM state WHERE user_id = ?", (user_id,)).fetchone()
    if s is None:
        return
    cur = s["current_streak"]
    earned = s["streak_freezes_earned"]
    if cur > 0 and cur % STREAK_FREEZE_PERIOD_DAYS == 0 and cur > earned:
        conn.execute(
            "UPDATE state SET streak_freezes_earned = ? WHERE user_id = ?",
            (cur, user_id),
        )


def update_streak_for_entry(user_id: int, entry_day: date) -> None:
    with get_db() as conn:
        s = conn.execute("SELECT * FROM state WHERE user_id = ?", (user_id,)).fetchone()
        if s is None:
            conn.execute("INSERT INTO state(user_id) VALUES (?)", (user_id,))
            s = conn.execute("SELECT * FROM state WHERE user_id = ?", (user_id,)).fetchone()
        last = s["last_entry_date"]
        cur = s["current_streak"]
        longest = s["longest_streak"]
        last_d = date.fromisoformat(last) if last else None
        if last_d is None:
            new_cur = 1
        elif entry_day == last_d:
            new_cur = cur
        elif entry_day == last_d + timedelta(days=1):
            new_cur = cur + 1
        elif entry_day > last_d + timedelta(days=1):
            available = s["streak_freezes_earned"] - s["streak_freezes_used"]
            gap_days = (entry_day - last_d).days - 1
            if available >= gap_days and gap_days >= 1:
                new_cur = cur + 1
                conn.execute(
                    "UPDATE state SET streak_freezes_used = streak_freezes_used + ? WHERE user_id = ?",
                    (gap_days, user_id),
                )
            else:
                new_cur = 1
        else:
            new_cur = cur
        new_longest = max(longest, new_cur)
        conn.execute(
            "UPDATE state SET current_streak = ?, longest_streak = ?, last_entry_date = ? WHERE user_id = ?",
            (new_cur, new_longest, entry_day.isoformat(), user_id),
        )
        _award_streak_freeze_if_due(conn, user_id)


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

STATUS_TEXT = {200: "OK", 302: "Found", 401: "Unauthorized", 404: "Not Found", 500: "Internal Server Error"}


def esc(s) -> str:
    if s is None:
        return ""
    return html.escape(str(s))


def render_page(body: str, user: dict | None, active: str) -> bytes:
    base = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
    username = esc(user["username"]) if user else ""
    base = base.replace("__USERNAME__", username)
    for tab in ("TODAY", "BOOKS", "STREAK", "REWARDS"):
        cls = f"__{tab}__"
        is_active = tab.lower() == active.upper()
        base = base.replace(cls, "active" if is_active else "")
    base = base.replace("__BODY__", body)
    return base.encode("utf-8")


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

ROUTES: list[tuple[str, re.Pattern, callable]] = []


def route(method: str, path: str):
    def deco(fn):
        ROUTES.append((method, re.compile(f"^{path}$"), fn))
        return fn
    return deco


def parse_cookies(header_value: str) -> dict[str, str]:
    if not header_value:
        return {}
    c = http.cookies.SimpleCookie(header_value)
    return {k: v.value for k, v in c.items()}


@route("GET", "/api/ol-search")
def ol_search(environ, user, form):
    """DEPRECATED — search now happens client-side via JS.
    Kept for backward compat if any old requests land here.
    """
    return json_response({"error": "deprecated", "results": []})


def parse_form(body: bytes, content_type: str) -> dict:
    if not body:
        return {}
    if "application/x-www-form-urlencoded" in content_type:
        parsed = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}
    if "multipart/form-data" in content_type:
        m = re.search(r"boundary=(.+?)(?:;|$)", content_type)
        if not m:
            return {}
        boundary = m.group(1).strip().strip('"').encode()
        out = {}
        for part in body.split(b"--" + boundary):
            if part in (b"", b"--\r\n", b"--"):
                continue
            head, _, body_part = part.partition(b"\r\n\r\n")
            body_part = body_part.rstrip(b"\r\n")
            h_text = head.decode("utf-8", errors="ignore")
            name_m = re.search(r'name="([^"]+)"', h_text)
            if name_m:
                out[name_m.group(1)] = body_part.decode("utf-8", errors="ignore")
        return out
    return {}


def get_session_user(environ: dict) -> dict | None:
    cookies = parse_cookies(environ.get("HTTP_COOKIE", ""))
    return lookup_session(cookies.get(SESSION_COOKIE, ""))


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def html_response(body: bytes, status: int = 200, extra_headers: list[tuple[str, str]] | None = None) -> tuple:
    headers = [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    return status, headers, body


def redirect(location: str, extra_headers: list[tuple[str, str]] | None = None) -> tuple:
    headers = [("Location", location), ("Content-Type", "text/plain"), ("Content-Length", "0")]
    if extra_headers:
        headers.extend(extra_headers)
    return 302, headers, b""


def json_response(data: dict, status: int = 200) -> tuple:
    import json
    body = json.dumps(data).encode("utf-8")
    headers = [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
        ("Access-Control-Allow-Origin", "*"),
    ]
    return status, headers, body


def serve_static(path: str) -> tuple | None:
    """Return a static file response, or None if path is not under static/."""
    if not path.startswith("/static/"):
        return None
    rel = path[len("/static/"):]
    safe = (STATIC_DIR / rel).resolve()
    if not str(safe).startswith(str(STATIC_DIR.resolve())):
        return None
    if not safe.is_file():
        return None
    body = safe.read_bytes()
    ctype = "text/css" if safe.suffix == ".css" else "application/octet-stream"
    return 200, [
        ("Content-Type", ctype),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "public, max-age=300"),
    ], body


# ---------------------------------------------------------------------------
# Page handlers
# ---------------------------------------------------------------------------

@route("GET", "/")
def home(environ, user, form):
    if user is None:
        return redirect("/login")
    with get_db() as conn:
        s = conn.execute("SELECT * FROM state WHERE user_id = ?", (user["user_id"],)).fetchone()
        books = conn.execute(
            "SELECT b.*, (SELECT COUNT(*) FROM entries e WHERE e.book_id = b.id) AS entry_count "
            "FROM books b WHERE user_id = ? AND status='reading' ORDER BY started_at DESC",
            (user["user_id"],),
        ).fetchall()
        today = date.today().isoformat()
        todays = conn.execute(
            "SELECT e.*, b.title AS book_title FROM entries e "
            "JOIN books b ON b.id = e.book_id "
            "WHERE e.user_id = ? AND e.entry_date = ? ORDER BY e.created_at DESC",
            (user["user_id"], today),
        ).fetchall()
        recent = conn.execute(
            "SELECT e.*, b.title AS book_title FROM entries e "
            "JOIN books b ON b.id = e.book_id "
            "WHERE e.user_id = ? ORDER BY e.created_at DESC LIMIT 8",
            (user["user_id"],),
        ).fetchall()
    s = dict(s) if s else {}
    books_html = "".join(
        f'<option value="{esc(b["id"])}">{esc(b["title"])}' + (f' — {esc(b["author"])}' if b["author"] else "") + '</option>'
        for b in books
    )
    if not books_html:
        books_html = '<option value="" disabled>— add a book first —</option>'

    todays_html = "".join(
        f'<li><div class="entry-meta"><strong>{esc(t["book_title"])}</strong> · +{t["points_awarded"]} pts · {esc(t["created_at"][11:16])}</div>'
        f'<div class="entry-body">{esc(t["body"])}</div></li>'
        for t in todays
    )
    recent_html = "".join(
        f'<li><div class="entry-meta"><strong>{esc(r["book_title"])}</strong> · {esc(r["entry_date"])} · +{r["points_awarded"]} pts</div>'
        f'<div class="entry-body">{esc(r["body"][:200])}{("…" if len(r["body"]) > 200 else "")}</div></li>'
        for r in recent
    )

    body = f'''
<div class="hero glass">
  <div class="streak-card">
    <div class="streak-num">{s.get("current_streak", 0)}</div>
    <div class="streak-label">day streak</div>
    <div class="streak-sub">best: {s.get("longest_streak", 0)} days · 🏅 {s.get("total_points", 0)} pts</div>
  </div>
  <div class="hero-text">
    <h1>Good morning, {esc(user["username"])}.</h1>
    <p>{esc(today)} — what did you learn today?</p>
  </div>
</div>

<section class="card glass">
  <h2>Add today's learning</h2>
  <form method="post" action="/entries/add" class="entry-form">
    <label for="book_id">Book</label>
    <select name="book_id" id="book_id" required>{books_html}</select>
    <label for="body">Your thought / learning</label>
    <textarea name="body" id="body" rows="5" required placeholder="Today I learned that…"></textarea>
    <div class="row">
      <small class="hint">+10 base · +2 per streak day · +25 first entry on a book</small>
      <button type="submit" class="primary">Save entry</button>
    </div>
  </form>
</section>
'''
    if todays_html:
        body += f'''
<section class="card glass">
  <h2>📝 Today's entries</h2>
  <ul class="entries">{todays_html}</ul>
</section>
'''
    if recent_html:
        body += f'''
<section class="card glass">
  <h2>Recent entries</h2>
  <ul class="entries">{recent_html}</ul>
</section>
'''
    return html_response(render_page(body, user, "today"))


@route("GET", "/login")
def login_get(environ, user, form):
    if user is not None:
        return redirect("/")
    body = '''
<div class="login-wrap">
  <div class="login-card glass-bright">
    <h1>🔥 Pageburn</h1>
    <p>Sign in to log your daily learnings.</p>
    <form method="post" action="/login">
      <input type="text" name="username" placeholder="Username" required autocomplete="username">
      <input type="password" name="password" placeholder="Password" required autocomplete="current-password">
      <button type="submit" class="primary" style="margin-top:0.5rem;">Sign in</button>
    </form>
  </div>
</div>
'''
    return html_response(render_page(body, None, "login"))


@route("POST", "/login")
def login_post(environ, user, form):
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if row is None or not verify_password(password, row["password_salt"], row["password_hash"]):
        body = '''
<div class="login-wrap">
  <div class="login-card glass-bright">
    <h1>🔥 Pageburn</h1>
    <div class="err">Invalid credentials.</div>
    <form method="post" action="/login">
      <input type="text" name="username" placeholder="Username" required>
      <input type="password" name="password" placeholder="Password" required>
      <button type="submit" class="primary" style="margin-top:0.5rem;">Sign in</button>
    </form>
  </div>
</div>
'''
        return html_response(render_page(body, None, "login"), status=401)
    token = make_session_token(row["id"])
    return redirect("/", extra_headers=[
        ("Set-Cookie", f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
    ])


@route("POST", "/logout")
def logout_post(environ, user, form):
    cookies = parse_cookies(environ.get("HTTP_COOKIE", ""))
    tok = cookies.get(SESSION_COOKIE, "")
    if tok:
        destroy_session(tok)
    return redirect("/login", extra_headers=[
        ("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; HttpOnly; Max-Age=0")
    ])


@route("GET", "/books")
def books_list(environ, user, form):
    if user is None:
        return redirect("/login")
    with get_db() as conn:
        books = conn.execute(
            "SELECT b.*, (SELECT COUNT(*) FROM entries e WHERE e.book_id = b.id) AS entry_count "
            "FROM books b WHERE user_id = ? ORDER BY CASE b.status WHEN 'reading' THEN 0 ELSE 1 END, b.started_at DESC",
            (user["user_id"],),
        ).fetchall()
    # Build cover HTML: real cover if available, else a gradient placeholder
    def cover_html(b):
        url = b.get("cover_url") if isinstance(b, dict) else (b["cover_url"] if hasattr(b, "__getitem__") else None)
        if url:
            title_esc = esc(b["title"])
            return f'<img class="book-cover-img" src="{esc(url)}" alt="{title_esc}" loading="lazy" onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'">'
        return f'<div class="book-cover-placeholder" style="display:flex"><span>{esc(b["title"][:1])}</span></div>'
    rows = "".join(
        f'<div class="book-row">'
        f'<div class="book-cover-wrap">{cover_html(b)}</div>'
        f'<div class="meta"><strong><a href="/books/{b["id"]}" style="color:var(--accent-2);text-decoration:none;">{esc(b["title"])}</a></strong>'
        f'<small>{(esc(b["author"]) + " · ") if b["author"] else ""}{b["entry_count"]} entries · started {esc(b["started_at"])}</small></div>'
        f'<span class="status-pill {esc(b["status"])}">{esc(b["status"])}</span>'
        f'<a class="btn-secondary" href="/books/{b["id"]}">Open</a>'
        f'</div>'
        for b in books
    )
    if not rows:
        rows = '<div class="empty">No books yet. Add your first one above.</div>'
    body = '''
<section class="card glass">
  <h2>Add a book</h2>
  <form method="post" action="/books/add" class="entry-form" id="add-book-form">
    <div style="display:flex;flex-direction:column;gap:0.75rem;">

      <!-- Title + Search -->
      <div style="display:flex;gap:0.5rem;align-items:flex-end;flex-wrap:wrap;position:relative;">
        <div style="flex:1;display:flex;flex-direction:column;gap:0.3rem;min-width:180px;">
          <label>Title</label>
          <input type="text" name="title" id="book-title" required
                 autocomplete="off" placeholder="Start typing...">
        </div>
        <button type="button" class="primary" id="ol-search-btn"
                style="padding:0.45rem 0.9rem;flex-shrink:0;display:flex;align-items:center;gap:0.4rem;">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
            <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
          </svg>
          Search OL
        </button>
        <!-- OL Search dropdown -->
        <div id="ol-dropdown" class="ol-dropdown hidden"></div>
      </div>

      <!-- Author -->
      <div style="display:flex;gap:0.75rem;flex-wrap:wrap;">
        <div style="flex:1;display:flex;flex-direction:column;gap:0.3rem;">
          <label>Author <span style="color:var(--muted);font-weight:400;">(optional — auto-filled)</span></label>
          <input type="text" name="author" id="book-author" placeholder="Author name">
        </div>
        <div style="flex:1;display:flex;flex-direction:column;gap:0.3rem;">
          <label>Cover URL <span style="color:var(--muted);font-weight:400;">(auto-filled or paste your own)</span></label>
          <input type="url" name="cover_url" id="book-cover-url" placeholder="https://...">
          <input type="hidden" name="work_key" id="book-work-key">
        </div>
      </div>

      <div style="display:flex;align-items:center;gap:0.5rem;">
        <button type="submit" class="primary" style="align-self:flex-start;">Add book</button>
        <span id="ol-status" style="color:var(--muted);font-size:0.85rem;"></span>
      </div>
    </div>
  </form>
</section>

<script>
(function() {
  const titleInput = document.getElementById('book-title');
  const authorInput = document.getElementById('book-author');
  const coverInput = document.getElementById('book-cover-url');
  const workKeyInput = document.getElementById('book-work-key');
  const searchBtn = document.getElementById('ol-search-btn');
  const dropdown = document.getElementById('ol-dropdown');
  const statusEl = document.getElementById('ol-status');

  let debounceTimer = null;

  // Live search as user types (300ms debounce)
  titleInput.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    if (titleInput.value.trim().length < 3) { dropdown.classList.add('hidden'); return; }
    debounceTimer = setTimeout(() => doSearch(titleInput.value.trim()), 300);
  });

  // Also allow explicit button click
  searchBtn.addEventListener('click', () => {
    const q = titleInput.value.trim();
    if (!q) { titleInput.focus(); return; }
    doSearch(q);
  });

  // Close dropdown when clicking outside
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#ol-dropdown') && !e.target.closest('#ol-search-btn')) {
      dropdown.classList.add('hidden');
    }
  });

  function doSearch(query) {
    dropdown.innerHTML = '<div style="padding:0.75rem;text-align:center;color:var(--muted);font-size:0.85rem;">Searching...</div>';
    dropdown.classList.remove('hidden');
    statusEl.textContent = 'Searching Open Library...';
    searchBtn.disabled = true;

    const url = `https://openlibrary.org/search.json?q=${
      encodeURIComponent(query)}&fields=key,title,author_name,cover_i,first_publish_year&limit=6`;

    fetch(url)
      .then(r => r.json())
      .then(data => renderResults(data.docs || []))
      .catch(() => {
        dropdown.innerHTML = '<div style="padding:0.75rem;color:#f87171;font-size:0.85rem;">Search failed — check your connection</div>';
        statusEl.textContent = '';
        searchBtn.disabled = false;
      });
  }

  function renderResults(docs) {
    searchBtn.disabled = false;
    statusEl.textContent = '';
    if (!docs.length) {
      dropdown.innerHTML = '<div style="padding:0.75rem;color:var(--muted);font-size:0.85rem;">No results found</div>';
      return;
    }
    dropdown.innerHTML = docs.map((doc, i) => {
      const title = doc.title || '';
      const author = (doc.author_name && doc.author_name[0]) || '';
      const year = doc.first_publish_year || '';
      const coverUrl = doc.cover_i
        ? `https://covers.openlibrary.org/b/id/${doc.cover_i}-S.jpg`
        : null;
      return `<div class="ol-result" data-index="${i}" style="display:flex;align-items:center;gap:0.75rem;padding:0.6rem 0.75rem;cursor:pointer;border-bottom:1px solid var(--glass-border);transition:background 0.15s;" onmouseover="this.style.background='rgba(255,255,255,0.06)'" onmouseout="this.style.background=''">
        ${
          coverUrl
            ? `<img src="${coverUrl}" alt="" style="width:32px;height:44px;object-fit:cover;border-radius:3px;flex-shrink:0;background:rgba(255,255,255,0.08);">`
            : `<div style="width:32px;height:44px;background:rgba(255,255,255,0.08);border-radius:3px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:1.1rem;">📖</div>`
        }
        <div style="flex:1;min-width:0;">
          <div style="font-weight:600;font-size:0.9rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escHtml(title)}</div>
          <div style="color:var(--muted);font-size:0.8rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escHtml(author)}${year ? ' · ' + year : ''}</div>
        </div>
        <div style="display:none;" class="ol-result-data"
          data-title="${escAttr(title)}"
          data-author="${escAttr(author)}"
          data-cover="${coverUrl ? escAttr(`https://covers.openlibrary.org/b/id/${doc.cover_i}-M.jpg`) : ''}"
          data-work="${escAttr(doc.key || '')}"
        ></div>
      </div>`;
    }).join('');

    // Attach click handlers
    dropdown.querySelectorAll('.ol-result').forEach(el => {
      el.addEventListener('click', () => {
        const data = el.querySelector('.ol-result-data');
        const t = data.dataset.title;
        const a = data.dataset.author;
        const c = data.dataset.cover;
        titleInput.value = t;
        authorInput.value = a;
        if (c) coverInput.value = c;
        const wk = data.dataset.work;
        if (wk) workKeyInput.value = wk;
        dropdown.classList.add('hidden');
        statusEl.textContent = a ? `✓ Found: ${t} by ${a}` : `✓ Found: ${t}`;
      });
    });
  }

  function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }
  function escAttr(s) {
    return s.replace(/"/g, '&quot;');
  }
})();
</script>

<section class="card glass">
  <h2>Your library</h2>
  {rows}
</section>
'''
    body = body.replace('{rows}', rows)
    body += '<div id="summary-section" class="card glass" style="display:none;"> \
      <h2 style="display:flex;align-items:center;gap:0.5rem;"> \
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"> \
          <path d="M4 6h16M4 12h12M4 18h8"/> \
        </svg> \
        Summary \
      </h2> \
      <div id="summary-loading" style="display:flex;align-items:center;gap:0.5rem;color:var(--muted);padding:0.5rem 0;"> \
        <div style="width:14px;height:14px;border:2px solid rgba(255,255,255,0.15);border-top-color:var(--accent);border-radius:50%;animation:spin 0.8s linear infinite;"></div> \
        <span style="font-size:0.85rem;">Loading from Open Library...</span> \
      </div> \
      <div id="summary-text" style="line-height:1.7;font-size:0.95rem;display:none;"></div> \
      <div id="summary-empty" style="color:var(--muted);font-size:0.85rem;display:none;">No summary available for this book.</div> \
      <div id="summary-error" style="color:var(--muted);font-size:0.85rem;display:none;"></div> \
    </div> \
    <script> \
    (function() { \
      var wk = document.querySelector(".detail-info")?.dataset?.workKey; \
      if (!wk) return; \
      var sec = document.getElementById("summary-section"); \
      var loading = document.getElementById("summary-loading"); \
      var textEl = document.getElementById("summary-text"); \
      var emptyEl = document.getElementById("summary-empty"); \
      var errEl = document.getElementById("summary-error"); \
      fetch("https://openlibrary.org" + wk + ".json") \
        .then(function(r) { if (!r.ok) throw new Error(r.status); return r.json(); }) \
        .then(function(data) { \
          loading.style.display = "none"; \
          var desc = data.description; \
          var txt = typeof desc === "string" ? desc : (desc && desc.value) || ""; \
          if (!txt) { emptyEl.style.display = "block"; return; } \
          textEl.innerHTML = txt; \
          sec.style.display = "block"; \
        }) \
        .catch(function() { \
          loading.style.display = "none"; \
          errEl.style.display = "block"; \
          errEl.textContent = "Could not load summary."; \
        }); \
    })(); \
    </scr" + "ipt> \
    </script>';
    return html_response(render_page(body, user, "books"))


@route("POST", "/books/add")
def books_add(environ, user, form):
    if user is None:
        return redirect("/login")
    title = (form.get("title") or "").strip()
    author = (form.get("author") or "").strip()
    if not title:
        return redirect("/books")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO books(user_id, title, author, cover_url, work_key, status, started_at) VALUES (?,?,?,?,?,?,?)",
            (user["user_id"], title, author or None, form.get("cover_url") or None,
             form.get("work_key") or None, "reading", date.today().isoformat()),
        )
        book_id = cur.lastrowid
    if form.get("cover_url"):
        pass  # cover already set
    else:
        _fetch_cover_async(book_id, title, author or None)
    return redirect("/books")


@route("POST", "/books/finish")
def books_finish(environ, user, form):
    if user is None:
        return redirect("/login")
    book_id = int(form.get("book_id") or "0")
    if not book_id:
        return redirect("/books")
    with get_db() as conn:
        b = conn.execute("SELECT * FROM books WHERE id=? AND user_id=?", (book_id, user["user_id"])).fetchone()
        if b is None or b["status"] == "finished":
            return redirect("/books")
        conn.execute(
            "UPDATE books SET status='finished', finished_at=? WHERE id=?",
            (date.today().isoformat(), book_id),
        )
        conn.execute(
            "UPDATE state SET total_points = total_points + ? WHERE user_id = ?",
            (POINTS_FINISH_BOOK, user["user_id"]),
        )
    return redirect(f"/books/{book_id}")



@route("GET", r"/books/(\d+)")
def book_detail(environ, user, form, m):
    if user is None:
        return redirect("/login")
    book_id = int(m.group(1))
    with get_db() as conn:
        b = conn.execute("SELECT * FROM books WHERE id=? AND user_id=?", (book_id, user["user_id"])).fetchone()
        if b is None:
            return redirect("/books")
        entries = conn.execute(
            "SELECT * FROM entries WHERE book_id=? ORDER BY created_at DESC", (book_id,)
        ).fetchall()
    finish_action = ""
    if b["status"] == "reading":
        finish_action = ("<form method=\"post\" action=\"/books/finish\" style=\"margin-top:1rem;display:inline-block;\">"
                         "<input type=\"hidden\" name=\"book_id\" value=\"" + str(book_id) + "\">"
                         "<button type=\"submit\" class=\"primary\">Mark as finished</button></form>")
    entries_html = "".join(
        "<li><div class=\"entry-meta\">" + esc(e["entry_date"]) + " · +" + str(e["points_awarded"]) + " pts</div>"
        "<div class=\"entry-body\">" + esc(e["body"]) + "</div></li>"
        for e in entries
    )
    if not entries_html:
        entries_html = "<div class=\"empty\">No entries yet for this book.</div>"
    cover_html = ""
    if b["cover_url"]:
        cover_html = "<div class=\"detail-cover-wrap\"><img class=\"detail-cover-img\" src=\"" + esc(b["cover_url"]) + "\" alt=\"" + esc(b["title"]) + "\" onerror=\"this.style.display=\'none\';this.parentElement.style.display=\'none'\"></div>"
    work_key = b["work_key"] or ""
    author_html = "<p class=\"detail-author\">" + esc(b["author"]) + "</p>" if b["author"] else ""
    status_html = "<span class=\"status-pill " + esc(b["status"]) + "\">" + esc(b["status"]) + "</span>"
    dates_html = "<span class=\"detail-dates\">Started " + esc(b["started_at"])
    if b["finished_at"]:
        dates_html += " · Finished " + esc(b["finished_at"])
    dates_html += "</span>"
    body = ("<section class=\"card glass\">"
            "<div class=\"detail-header\">"
            + cover_html +
            "<div class=\"detail-info\" data-work-key=\"" + work_key + "\">"
            "<h2 class=\"detail-title\">" + esc(b["title"]) + "</h2>"
            + author_html +
            "<div class=\"detail-meta\">"
            + status_html + dates_html +
            "</div>"
            + finish_action +
            "</div>"
            "</div>"
            "</section>"
            "<section class=\"card glass\" id=\"summary-card\" style=\"display:none;\">"
            "<h2 style=\"display:flex;align-items:center;gap:0.5rem;\">"
            "<svg width=\"16\" height=\"16\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><path d=\"M4 6h16M4 12h12M4 18h8\"/></svg>"
            "Summary"
            "</h2>"
            "<div id=\"summary-loading\" style=\"display:flex;align-items:center;gap:0.5rem;color:var(--muted);padding:0.5rem 0;\">"
            "<div style=\"width:14px;height:14px;border:2px solid rgba(255,255,255,0.15);border-top-color:var(--accent);border-radius:50%;animation:spin 0.8s linear infinite;\"></div>"
            "<span style=\"font-size:0.85rem;\">Loading from Open Library...</span>"
            "</div>"
            "<div id=\"summary-text\" style=\"line-height:1.7;font-size:0.95rem;display:none;\"></div>"
            "<div id=\"summary-empty\" style=\"color:var(--muted);font-size:0.85rem;display:none;\">No summary available for this book.</div>"
            "<div id=\"summary-error\" style=\"color:var(--muted);font-size:0.85rem;display:none;\"></div>"
            "</section>"
            "<section class=\"card glass\">"
            "<h2>Entries</h2>"
            "<ul class=\"entries\">" + entries_html + "</ul>"
            "</section>")
    body += "<scr" + "ipt>"
    body += "(function(){"
    body += "var wk=document.querySelector('.detail-info')?.dataset?.workKey;"
    body += "if(!wk){document.getElementById('summary-card')?.remove();return;}"
    body += "var sec=document.getElementById('summary-card');"
    body += "var loading=document.getElementById('summary-loading');"
    body += "var textEl=document.getElementById('summary-text');"
    body += "var emptyEl=document.getElementById('summary-empty');"
    body += "var errEl=document.getElementById('summary-error');"
    body += "fetch('https://openlibrary.org'+wk+'.json')"
    body += ".then(function(r){if(!r.ok)throw Error(r.status);return r.json();})"
    body += ".then(function(d){"
    body += "loading.style.display='none';"
    body += "var desc=d.description;"
    body += "var txt=typeof desc==='string'?desc:(desc&&desc.value)||'';"
    body += "if(!txt){emptyEl.style.display='block';return;}"
    body += "textEl.innerHTML=txt;"
    body += "sec.style.display='block';"
    body += "})"
    body += ".catch(function(){loading.style.display='none';errEl.style.display='block';errEl.textContent='Could not load summary.';});"
    body += "})();"
    body += "</scr" + "ipt>"
    return html_response(render_page(body, user, "books"))

@route("POST", "/entries/add")
def entries_add(environ, user, form):
    if user is None:
        return redirect("/login")
    body_text = (form.get("body") or "").strip()
    book_id = int(form.get("book_id") or "0")
    if not body_text or not book_id:
        return redirect("/")
    with get_db() as conn:
        b = conn.execute("SELECT * FROM books WHERE id=? AND user_id=?", (book_id, user["user_id"])).fetchone()
        if b is None or b["status"] != "reading":
            return redirect("/")
        points, _breakdown = compute_entry_points(conn, user["user_id"], book_id)
        now = datetime.now(timezone.utc)
        entry_day = now.date().isoformat()
        conn.execute(
            "INSERT INTO entries(user_id, book_id, body, created_at, entry_date, points_awarded) "
            "VALUES (?,?,?,?,?,?)",
            (user["user_id"], book_id, body_text, now.isoformat(), entry_day, points),
        )
        conn.execute(
            "UPDATE state SET total_points = total_points + ? WHERE user_id = ?",
            (points, user["user_id"]),
        )
    update_streak_for_entry(user["user_id"], now.date())
    return redirect("/")


@route("GET", "/streak")
def streak_page(environ, user, form):
    if user is None:
        return redirect("/login")
    with get_db() as conn:
        s = conn.execute("SELECT * FROM state WHERE user_id = ?", (user["user_id"],)).fetchone()
        days = []
        today = date.today()
        for i in range(89, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM entries WHERE user_id = ? AND entry_date = ?",
                (user["user_id"], d),
            ).fetchone()["n"]
            if n == 0:
                lvl = 0
            elif n == 1:
                lvl = 1
            elif n == 2:
                lvl = 2
            elif n == 3:
                lvl = 3
            elif n == 4:
                lvl = 4
            else:
                lvl = 5
            days.append({"date": d, "count": n, "level": lvl})
    s = dict(s) if s else {}
    freezes_available = s.get("streak_freezes_earned", 0) - s.get("streak_freezes_used", 0)
    cells = "".join(
        f'<div class="heat-cell" data-l="{d["level"]}"><span class="tip">{d["date"]} — {d["count"]} entr{"y" if d["count"]==1 else "ies"}</span></div>'
        for d in days
    )
    body = f'''
<section class="card glass">
  <h2>Streak</h2>
  <div class="stat-grid">
    <div class="stat"><div class="v">{s.get("current_streak", 0)}</div><div class="l">current streak (days)</div></div>
    <div class="stat"><div class="v">{s.get("longest_streak", 0)}</div><div class="l">longest streak</div></div>
    <div class="stat"><div class="v">&#10052; {freezes_available}</div><div class="l">freezes available</div></div>
    <div class="stat"><div class="v">{s.get("total_points", 0)}</div><div class="l">total points</div></div>
  </div>
  <p style="color:var(--muted);margin-top:1rem;font-size:0.9rem;">Every 7-day streak earns you a &#10052; freeze that auto-protects a missed day. Hover the calendar to see daily counts.</p>
  <h3 style="margin-top:1.5rem;color:var(--accent-2);">Last 90 days</h3>
  <div class="heatmap">{cells}</div>
  <div class="legend"><span>less</span>
    <div class="heat-cell" data-l="0"></div>
    <div class="heat-cell" data-l="1"></div>
    <div class="heat-cell" data-l="2"></div>
    <div class="heat-cell" data-l="3"></div>
    <div class="heat-cell" data-l="4"></div>
    <div class="heat-cell" data-l="5"></div>
    <span>more</span>
  </div>
</section>
'''
    return html_response(render_page(body, user, "streak"))


@route("GET", "/rewards")
def rewards_page(environ, user, form):
    if user is None:
        return redirect("/login")
    with get_db() as conn:
        s = conn.execute("SELECT * FROM state WHERE user_id = ?", (user["user_id"],)).fetchone()
        total_entries = conn.execute(
            "SELECT COUNT(*) AS n FROM entries WHERE user_id = ?", (user["user_id"],)
        ).fetchone()["n"]
        total_books_finished = conn.execute(
            "SELECT COUNT(*) AS n FROM books WHERE user_id = ? AND status='finished'", (user["user_id"],)
        ).fetchone()["n"]
    s = dict(s) if s else {}
    milestones = [
        (50, "📖 First pages"),
        (150, "📚 Chapter champ"),
        (300, "🔥 Devoted reader"),
        (500, "🧠 Insight machine"),
        (1000, "🏆 Bookworm legend"),
    ]
    current_points = s.get("total_points", 0)
    items = []
    next_m = None
    for pts, label in milestones:
        achieved = current_points >= pts
        cls = "milestone achieved" if achieved else "milestone"
        badge = "✓ unlocked" if achieved else f"{pts - current_points} pts to go"
        if not achieved and next_m is None:
            cls = "milestone next"
            next_m = (pts, label)
        items.append(f'<div class="{cls}"><div class="pts">{pts} pts</div><div class="label">{esc(label)}</div><div class="badge">{esc(badge)}</div></div>')
    if next_m is None:
        next_m = (None, "All milestones unlocked! 🎉")
    items_html = "".join(items)
    body = f'''
<section class="card glass">
  <h2>Rewards</h2>
  <div class="stat-grid">
    <div class="stat"><div class="v">{current_points}</div><div class="l">total points</div></div>
    <div class="stat"><div class="v">{total_entries}</div><div class="l">entries logged</div></div>
    <div class="stat"><div class="v">{total_books_finished}</div><div class="l">books finished</div></div>
    <div class="stat"><div class="v">{s.get("longest_streak", 0)}</div><div class="l">longest streak</div></div>
  </div>
</section>

<section class="card glass">
  <h2>Milestones</h2>
  {items_html}
</section>
'''
    return html_response(render_page(body, user, "rewards"))


# ---------------------------------------------------------------------------
# WSGI entry
# ---------------------------------------------------------------------------

class NoLogHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")
    try:
        length = int(environ.get("CONTENT_LENGTH") or "0")
    except ValueError:
        length = 0
    body = environ["wsgi.input"].read(length) if length else b""
    content_type = environ.get("CONTENT_TYPE", "")
    form = parse_form(body, content_type) if method == "POST" else {}
    user = get_session_user(environ)

    # Static files
    static = serve_static(path)
    if static is not None:
        status, headers, payload = static
        start_response(f"{status} {STATUS_TEXT.get(status, '')}", headers)
        return [payload]

    for m, pattern, handler in ROUTES:
        if m != method:
            continue
        match = pattern.match(path)
        if match is None:
            continue
        try:
            if re.search(r"\(\?P|\\d", pattern.pattern) or "(" in pattern.pattern:
                result = handler(environ, user, form, match)
            else:
                result = handler(environ, user, form)
        except Exception as e:
            sys.stderr.write(f"[error] {handler.__name__}: {e!r}\n")
            import traceback
            traceback.print_exc()
            result = html_response(b"<h1>500 Internal Server Error</h1>", status=500)
        status, headers, payload = result
        start_response(f"{status} {STATUS_TEXT.get(status, '')}", headers)
        return [payload]

    body = b"<h1>404 Not Found</h1><p><a href='/'>Home</a></p>"
    status, headers, payload = html_response(body, status=404)
    start_response("404 Not Found", headers)
    return [payload]


def main():
    init_db()
    print(f"[pageburn] listening on http://{HOST}:{PORT}/")
    print(f"[pageburn] db: {DB_PATH}")
    print(f"[pageburn] default login: mohit / reading  (change after first login)")
    with make_server(HOST, PORT, application, handler_class=NoLogHandler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n[pageburn] shutting down")


if __name__ == "__main__":
    main()
