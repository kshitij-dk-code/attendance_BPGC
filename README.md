# Attendance App

A session-based classroom attendance system. The instructor opens an admin
dashboard, starts a session, and projects a QR code; students scan it with
their phones, sign in with their college Google account, (optionally) answer
the Google Form's questions **in-app** (native quiz), and are recorded in a
per-session SQLite/PostgreSQL database. The instructor
stops the session and downloads the attendance as CSV.

Built with **FastAPI + Jinja2 + SQLite + Google OAuth** — no frontend build
step, no external database.

---

## Features

- **Admin dashboard** (`/admin`) with server-verified login (session cookie;
  wrong password never sees the dashboard; login expires 10 minutes after
  entry — a refresh then returns to the login screen)
  - Session config: Google Form URL, form timeout, WiFi IP restriction,
    QR refresh rate — each pre-filled from an env default (see Configuration)
  - Live QR code that auto-refreshes every 45 s (configurable lifespan)
  - One-click start/stop; stop streams the session's attendance as **CSV**
  - Live "what IP does the server see" hint for configuring the WiFi check
- **QR scan flow** (`/scan`)
  - Signed, time-limited tokens (`itsdangerous`) — one QR per refresh
  - Same QR may be scanned by **many different devices**; a single device
    can only consume a QR once
  - Permanent per-device fingerprint cookie (stops phone-passing)
- **Google OAuth login** restricted to one email domain
  - `@goa.bits-pilani.ac.in` by default (`ALLOWED_EMAIL_DOMAIN`)
- **Optional WiFi restriction** — client-side WebRTC local-IP detection with
  public-IP fallback (the server sits behind NAT and usually cannot see the
  phone's LAN IP)
- **Optional Google Form step** — **native quiz** by default: the app reads
  the form's questions and renders them itself, hides the Email question,
  and forwards the answers to the same Google Form server-side with the
  OAuth-verified email (no account chooser, no editable email). Unsupported
  or sign-in-required forms fall back to the embedded iframe. Timeout
  fallback records "False (Timeout)"
- **Anti-sharing enforcement** — DB `UNIQUE` on both `email` and `device_id`:
  one mark per student, one mark per device
- **Per-session storage** — every start creates a fresh PostgreSQL table
  (`attendance_YYYY_MM_DD_HH_MM`) when `DATABASE_URL` is set (Replit's
  Database tool), or an `Attendance_YYYY_MM_DD_HH_MM.db` SQLite file next to
  `main.py` locally
- **Modern dark UI** — glassmorphism, responsive, works on cheap phones

## Tech stack

| Layer     | Choice |
|-----------|--------|
| Framework | FastAPI + Uvicorn |
| Templates | Jinja2 |
| Database  | PostgreSQL when `DATABASE_URL` is set (Replit), else SQLite (file per session) |
| Auth      | Authlib (Google OIDC) |
| Tokens    | itsdangerous (signed, timed) |
| Sessions  | Starlette SessionMiddleware |

---

## Quick start

```bash
# 1. Create and activate a virtualenv (Python 3.11+)
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Configure (minimum required: Google OAuth credentials)
export ADMIN_PASSWORD="choose-a-strong-password"
export YOUR_GOOGLE_CLIENT_ID="....apps.googleusercontent.com"
export YOUR_GOOGLE_CLIENT_SECRET="...."
# Optional Session Config pre-fills:
# export DEFAULT_TIMER="45"               # QR refresh secs (blank -> 45)
# export DEFAULT_FORM_URL="https://..."   # Google Form URL (blank -> none)

# 3. Run
uvicorn main:app --reload          # dev
# or
python main.py                     # dev, binds 0.0.0.0:8000
```

Open <http://localhost:8000/admin>.

### Google OAuth setup

1. Go to Google Cloud Console → **APIs & Services → Credentials**.
2. Create an **OAuth 2.0 Client ID** (type: Web application).
3. Authorized redirect URI: `http://localhost:8000/auth/callback`
   (use your deployed origin in production).
4. Export the client ID/secret as shown above.

---

## Configuration

All configuration is via environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `ADMIN_PASSWORD` | `admin123` | Admin password — checked by `POST /api/admin/login` (sets a session cookie) or the `X-Admin-Password` header |
| `YOUR_GOOGLE_CLIENT_ID` | `google_client_id_here` | Google OAuth client ID |
| `YOUR_GOOGLE_CLIENT_SECRET` | `google_client_secret_here` | Google OAuth client secret |
| `DEFAULT_ALLOWED_IP` | `10.20.1.92` | WiFi-IP prefix **pre-filled** in Session Config (editable/clearable per session) |
| `DEFAULT_TIMER` | *(blank → `45`)* | QR refresh rate (secs) **pre-filled** in Session Config; blank/non-numeric falls back to 45 |
| `DEFAULT_FORM_URL` | *(blank)* | Google Form URL **pre-filled** in Session Config; blank stays blank (no form step) |
| `DEFAULT_FORM_TIMEOUT` | *(blank → `300`)* | Form timeout (secs) **pre-filled** in Session Config; blank/non-numeric falls back to 300 |
| `DATABASE_URL` | *(unset)* | PostgreSQL connection string. Replit's Database tool sets it automatically; when present, attendance is stored there instead of SQLite |
| `ALLOWED_EMAIL_DOMAIN` | `@goa.bits-pilani.ac.in` | *(set in `main.py`)* which Google accounts may mark attendance |
| `SECRET_KEY` | *(set in `main.py`)* | Signs tokens & sessions — **change for production** |

> **Note:** `ALLOWED_EMAIL_DOMAIN` and `SECRET_KEY` are constants at the top of
> `main.py` (lines 20 and 19).

---

## Storage

- **Replit (PostgreSQL):** when `DATABASE_URL` is set — the Database tool
  provisions it automatically — each session writes to its own table named
  `attendance_YYYY_MM_DD_HH_MM`. Open **Database → My Data** in the workspace
  to browse the rows. The Database pane shows PostgreSQL only; it never lists
  SQLite files.
- **Local / no `DATABASE_URL`:** each session creates
  `Attendance_YYYY_MM_DD_HH_MM.db` **next to `main.py`**, regardless of the
  working directory.
- Either way, **Stop Session** downloads the same CSV.

---

## Usage

### Instructor
1. Open `/admin`, enter the admin password. A wrong password stays on the
   login screen — the dashboard HTML (and its prefills) is only served
   after the server verifies the password. **Log out** (top-right) clears
   the session immediately; it does not stop a running attendance session.
2. Fill Session Config (env defaults are pre-filled where set):
   - **Google Form URL** — from `DEFAULT_FORM_URL`; leave empty to skip
     the form step entirely. At start the app fetches the form and chooses a
     mode:
     - **Native quiz (green)** — the form is public, single-page, uses
       short-answer/paragraph/multiple-choice/dropdown/checkbox/scale
       questions (no grids/dates/times/"Other"), and has a short-answer
       **Email** question (mark it required for the embedded path).
       Students answer the questions in-app; the Email question is hidden
       and filled server-side from the OAuth session, and the answers are
       forwarded to the same Google Form (so the Sheet still receives them;
       grading/feedback stays in Google Forms). Native mode requires the
       form settings **Do not collect email addresses**, no **Limit to 1
       response**, and no org restriction.
     - **Embedded fallback (amber/red)** — anything else (multi-page,
       unsupported question types, "Other", sign-in required, no Email
       question). The inline verdict under the URL gives the reason; the
       iframe flow and email prefill still work as before.
     The attendance database/CSV is always the verified identity source.
     (A manual `{email}` placeholder / `entry.<id>=` param still works for
     the embedded path.)
   - **Form Timeout** — seconds a student gets to fill the form (enabled
     once a form URL is present); defaults to `DEFAULT_FORM_TIMEOUT` (300 s).
   - **WiFi IP Prefix** — pre-filled from `DEFAULT_ALLOWED_IP`; edit it,
     comma-separate several prefixes (`10.20.1.,47.29.`), or clear the field
     to disable the network restriction. iPhones can't report a local WiFi
     IP, so they are matched by the instructor's public IP automatically.
   - **QR Refresh Rate** — from `DEFAULT_TIMER` (falls back to 45 s).
3. Click **Start Session** — the QR appears and refreshes at the configured
   rate (45 s by default).
4. Project the QR. Students scan → Google sign-in → (form) → done.
5. **Stop Session** downloads `Attendance_....csv`.

### Student
1. Scan the QR → optional WiFi check page → redirected to Google.
2. Sign in with the allowed college account.
3. If a form is configured:
   - **Native quiz:** answer the questions on the app page. The Email
     question is not shown — your verified college email is attached
     automatically. Submit before the timer; attendance is recorded only
     after Google accepts the forwarded response.
   - **Embedded fallback:** submit the iframe form (verified email
     pre-filled). Attendance is marked automatically once Google records
     the response; if the form requires sign-in, a **Confirm Attendance**
     button appears instead.
   You cannot be marked without submitting — if you run out of time, the
   timer records `False (Timeout)`.
4. Success screen shows the recorded email.

---

## Project structure

```
.
├── main.py                    # FastAPI app — all routes and logic
├── templates/
│   ├── admin_dashboard.html   # Admin UI: login, QR, session config
│   ├── quiz.html              # Native quiz: questions + timer + submit
│   ├── form.html              # Fallback embedded Google Form + timer + confirm
│   ├── network_check.html     # WebRTC WiFi verification interstitial
│   └── status.html            # Success/error screen for students
├── loadtest/locustfile.py     # Realistic load test (see below)
├── requirements.txt
├── run.txt                    # 2-line run reminder
├── venv/                      # Virtualenv
└── README.md / WORK.md
```

## API reference

Admin endpoints accept either the browser **session cookie** (set by
`POST /api/admin/login`) or the **`X-Admin-Password` header** (scripts).
`GET /admin` serves only the login screen until the session is authenticated.

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/admin` | Admin dashboard HTML (login screen until authenticated) |
| POST | `/api/admin/login` | Verify password → sets admin session cookie (401 on wrong password) |
| POST | `/api/admin/logout` | Clear the admin session cookie |
| POST | `/api/admin/start` | Start session → creates session DB (500 detail names the storage error) |
| GET  | `/api/admin/diagnostics` | Storage health: mode, `DATABASE_URL` presence, psycopg, connect test |
| POST | `/api/admin/stop` | Stop session → returns CSV, deactivates |
| POST | `/api/admin/inspect-form` | Preview form mode (native quiz / embedded reason / email question) |
| GET  | `/api/admin/generate-qr-token` | New signed QR token (`?lifespan=45`) |
| GET  | `/api/check-ip` | Server-observed client IP + match status |
| GET  | `/scan?token=...` | Student entry point |
| POST | `/api/verify-network` | Verify WebRTC local IP / public IP |
| GET  | `/login` → `/auth/callback` | Google OAuth flow |
| POST | `/finalize-attendance` | Record attendance after the form |
| POST | `/api/quiz/submit` | Native quiz: validate answers, forward to Google, record attendance |
| POST | `/api/form/confirm` | Mark the embedded form as submitted |
| POST | `/api/form/status` | Poll confirmation state |

---

## Load test

A realistic simulation lives in `loadtest/locustfile.py`: admins refresh the
QR while students burst-scan the *same* QR and finalize attendance.

```bash
pip install locust

# Realistic classroom (students have think-time)
locust -f loadtest/locustfile.py --headless \
    -u 50 -r 10 -t 45s --host http://127.0.0.1:8000 --only-summary

# Raw server ceiling (no think-time)
LOCUST_NO_WAIT=1 locust -f loadtest/locustfile.py --headless \
    -u 50 -r 10 -t 30s --host http://127.0.0.1:8000 --only-summary
```

Measured on the dev box (2 vCPU / 4 GB, single uvicorn worker):

| Scenario | Users | Throughput | p50 | p95 | Errors |
|----------|------:|-----------:|----:|----:|-------:|
| Classroom (think-time) | 50 | 67 req/s | 9 ms | 27 ms | 0 |
| Heavy | 200 | 243 req/s | 25 ms | 320 ms | 0 |
| Saturated (think-time) | 500 | 253 req/s | 1.0 s | 2.3 s | 0 |
| Raw ceiling (no think-time) | 50–300 | ~258 req/s* | 160 ms | 430 ms | 0 |
| Sequential probe (no load) | 1 | **1.7 ms/req** | — | — | — |

\* test-client bound; the server's sequential 1.7 ms implies a single-loop
ceiling near **500–600 req/s**. A class of ~300 students peaks at well under
**20 req/s** — this hardware is ~10–50× beyond real-world demand.
Full analysis and bottleneck discussion: see `WORK.md`.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `command not found: uvicorn` after moving/renaming the project folder | venvs embed absolute paths — recreate it: `python3 -m venv venv --clear && pip install -r requirements.txt` |
| Second phone gets "already scanned" | You are running **old code** — `consumed_tokens` must be a dict keyed by `(nonce, device_id)`. Restart/redeploy. |
| "Use your @... account" | Signed into the wrong Google account. |
| Form asks to choose/sign in with a Google account | In the form settings: **Do not collect email addresses**, turn off **Limit to 1 response** and org restrictions, and add a short-answer **Email** question. The app detects and pre-fills it, so no account chooser is needed. |
| Inline check says **embedded mode** instead of native quiz | Hover/read the reason: multi-page form, unsupported question type (grid/date/time), an "Other" option, sign-in required, or no Email question. Make the form single-page with basic question types and add a required short-answer **Email** question; otherwise the iframe fallback is used. |
| Native quiz submit fails (502) | Google did not accept the forwarded response (rate-limit/captcha/wording change). Ask the student to retry; if it persists, stop the session and restart with a form that forces the embedded fallback (e.g. add a page break or an "Other" option). `formResponse` is an undocumented endpoint. |
| Form response shows a different email | The form records its own account/question. Fix the form settings above; the attendance database always stores the OAuth-verified email regardless. |
| Embedded form shows my personal Gmail | That chip is the phone browser's Google session, not the recorded email. If it says **Not shared**, the response is anonymous — add a short-answer **Email** question (the app prefills the college email). If it collects email, switch to **Do not collect**. |
| Late form submission after the timer | Google Forms cannot be time-locked. The app rejects late attendance confirmation and records `False (Timeout)`; ignore quiz responses timestamped after the deadline. |
| "Device not verified" | Cookies blocked, or the QR expired before finishing — scan again. |
| WiFi check fails on cellular/VPN | Prefix check needs the campus WiFi; clear the IP field to disable the restriction. |
| iPhone says WiFi IP "not detected" | Expected — iOS hides local IPs. The public IP is used instead (same campus network as the instructor passes automatically); clear the IP field if it still blocks. |
| `no such table: attendance` | Fixed — session must be active to finalize; restart if you see raw 500s. |
| Attendance not visible in Replit's **Database** pane | The pane lists PostgreSQL only. Provision the Database tool (sets `DATABASE_URL`) and restart the app; SQLite mode never appears there. |
| "Start failed" on Replit | The toast now shows the HTTP status + storage error. Open `/api/admin/diagnostics` after logging in: `db_connect_ok: false` with `db_error` means the managed Postgres is unreachable/misconfigured; check the Database tool and restart the app. |

## License

Private / course project.
