# Pageburn — Reading Tracker

A personal book reading tracker with Open Library integration. Tracks your reading sessions, streaks, points, and fetches book covers and descriptions automatically.

**Live at:** http://localhost:8765 (default credentials: `mohit` / `reading`)

---

## Features

- **Library management** — add books manually or search Open Library's database of millions of titles
- **Open Library integration** — search by title/author, auto-fetch covers and book descriptions
- **Reading sessions** — log entries against a book (page range or notes), earn points per entry
- **Streaks & gamification** — daily streak tracking, freeze protection, milestone badges, leaderboard-style points
- **Glass morphism UI** — dark, iOS-inspired glass design, fully responsive

---

## Quick Start

```bash
# No dependencies — pure Python 3 stdlib
python3 app/server.py
```

Open http://localhost:8765 — default login is `mohit` / `reading`.

To change host/port:
```bash
PAGEBURN_HOST=0.0.0.0 PAGEBURN_PORT=8765 python3 app/server.py
```

---

## Project Structure

```
pageburn/
├── app/
│   ├── server.py       # All application logic (WSGI app, routes, DB)
│   ├── static/
│   │   └── app.css     # Styles (glass morphism design)
│   └── templates/
│       ├── base.html   # Shared HTML base + nav
│       └── home.html   # Today's reading entry form
└── README.md
```

---

## How It Works

**Books** are added via the `/books` page. The "Search OL" button queries [Open Library's Search API](https://openlibrary.org/search.json) client-side — no API key needed. Selecting a result auto-fills:
- Title
- Author
- Cover image URL
- Open Library work key (used to fetch the book summary)

**Summaries** are fetched client-side from Open Library's works API when you open a book detail page.

**Entries** are reading session logs — each entry records notes or page range and awards points.

**Streaks** accrue one per day you log an entry. A freeze protects a missed day. Streaks and freezes reset on the 1st of each month.

---

## Tech Stack

- **Python 3** standard library only — `wsgiref`, `sqlite3`, `http.server`, `html`, `urllib`, `threading`, `http.cookies`
- **No pip, no venv, no external packages**
- Single `server.py` file (~1,200 lines) containing the entire application
- Open Library's free, keyless API for covers and book data
