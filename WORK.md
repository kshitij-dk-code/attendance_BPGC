# WORK.md — Onboarding & Engineering Log

**Read this file first if you are picking up this project.** It explains how
the system actually works, every significant change made, the traps that cost
hours, and how to verify your own changes. README has the "how to run it";
this has the "why it is the way it is".

---

## 0. Start here — 60-second bootstrap

```bash
cd /home/kshitij/Documents/Code/attendance-app/github_safe/opencode_workspace
source venv/bin/activate          # if commands fail, see §1.1 — recreate the venv
uvicorn main:app --reload         # dev server on :8000
```

- Admin UI: <http://localhost:8000/admin> (password: `ADMIN_PASSWORD`, default `admin123`)
- Python is **3.14**; all deps are pinned in `requirements.txt`.
- Before shipping any change: `python -c "import main"` (or
  `python3 -m py_compile main.py`) plus one manual flow (§6).
- **After editing, restart the deployment.** Stale running code caused a full
  debugging detour — see §5.1.

---

## 1. Environment gotchas (read before anything breaks)

### 1.1 The folder was renamed (`opencode_working` → `opencode_workspace`)
Python venvs bake **absolute paths** into `venv/bin/*` shebangs and into
`activate`. Symptoms after a rename:

- `venv/bin/pip install X` silently installs into a *different* venv (its
  shebang still points at an old/parent interpreter), so
  `venv/bin/python -m pip install` is the reliable variant.
- `source venv/bin/activate && uvicorn ...` may launch an interpreter that is
  not this folder's.
- **Fix:** `python3 -m venv venv --clear && venv/bin/python -m pip install -r requirements.txt`

### 1.2 `pkill -f "uvicorn main:app"` kills your own shell
`pkill -f` matches the *bash command line you are currently running*, which
contains that same string — it SIGKILLs itself and the tool hangs until
timeout with no output. Always use the bracket trick so the pattern cannot
match itself:

```bash
pkill -f "[u]vicorn main:app"      # matches the server, not this shell
pgrep -af "[u]vicorn"              # safe inspection
```

### 1.3 locust is NOT in the project venv
It landed in the parent `attendance-app/venv` (see §1.1). Run it as:

```bash
/home/kshitij/Documents/Code/attendance-app/venv/bin/python -m locust -f loadtest/locustfile.py ...
```

### 1.4 Misc
- `os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"` is set at import time —
  required for local HTTP OAuth; keep it.
- There is **no test suite**. Verification = import check + scripted
  simulation (§6) + manual flow.
- Screenshots (`Screenshot_*.png`, `Pasted image*.png`) in the project root
  are user-provided debug artifacts, not code.

---

## 2. How the system works (the mental model)

### 2.1 Lifecycle of one attendance mark

```
ADMIN                          SERVER                         STUDENT
─────                          ──────                         ───────
POST /api/admin/login  ──────▶ verify password → session cookie (admin_authed)
POST /api/admin/start  ──────▶ creates session storage (PG table / SQLite file)
                               session_state.is_active=True
GET generate-qr-token  ◀────── signed token {nonce, lifespan}
(project QR ────────────────────────────────────────────────▶ GET /scan?token=…
                                                               │ validate nonce age
                                                               │ device_id cookie (1 yr)
                                                               │ consumed once per (nonce, device)
                     ◀────── if allowed_ip: network_check page ─┘
                               POST /api/verify-network
                               (WebRTC local IP ∨ public IP)
                     ◀────── 307 /login + cookies
GET /login ──────────────────▶ Google OAuth redirect
                               /auth/callback
                               • domain check on email
                               • no form_url → INSERT → status ✓
                               • form_url → render form.html
                               POST /api/form/confirm (button)
                               POST /finalize-attendance → INSERT → status ✓
POST /api/admin/stop   ──────▶ is_active=False, streams CSV
```

### 2.2 State: three in-memory structures (**single-process only!**)

| Structure | Key/Value | Meaning |
|-----------|-----------|---------|
| `session_state` | dict | current session: active flag, form URL, timeout, allowed IP, instructor's public IP (`admin_public_ip`), **db file** |
| `consumed_tokens` | `(nonce, device_id) → timestamp` | QR consumption; entries pruned after 24 h (`CONSUMED_MAX_AGE`) |
| `form_confirmed` | set of emails | whether the student pressed the form-confirm button |

**Consequence: never run `uvicorn --workers N`.** Each worker would have its
own copies → QR/session state silently diverges. Single worker is a
requirement until this state moves to SQLite/Redis. (Load-test §7.)

### 2.3 Token & cookie semantics

| Token/cookie | Signed by | Max age | Carries |
|--------------|-----------|---------|---------|
| `session` (admin login) | SessionMiddleware (SECRET_KEY) | browser session; auth valid 10 min after login; `POST /api/admin/logout` clears it immediately | `admin_authed`, `admin_authed_at` |
| QR `token` (query) | `serializer` (SECRET_KEY) | `lifespan` inside payload (45 s default), serializer cap 86400 | `nonce`, `lifespan` |
| `device_session` cookie | serializer | 300 s | `device_id`, observed IP |
| `device_id` cookie | random hex(16) | 1 year | the permanent device fingerprint |
| `net_token` (network check form) | serializer | 300 s | `device_id`, public IP |
| form `form_token` | serializer | 1800 s | `email`, `device_id` |

- `nonce = str(time.time())` — expiry is `now - nonce > lifespan`.
- A QR is **per-device single-use but multi-device**: two students may scan
  the same displayed QR; only the *same* `device_id` is blocked from
  re-scanning it (§5.1).

### 2.4 Database rules (by design — do not "fix" these)

Every session gets its own storage target:

- **`DATABASE_URL` set (Replit):** a fresh PostgreSQL table
  `attendance_<Y_M_D_H_M>` (`SERIAL` id) in that database — this is what
  makes the rows show up in Replit's **Database → My Data** pane.
- **No `DATABASE_URL` (local):** a fresh
  `Attendance_<Y_M_D_H_M>.db` file **next to `main.py`** (anchored via
  `BASE_DIR`, independent of the working directory).

Both use the same schema:

```sql
attendance(
  id INTEGER PRIMARY KEY / SERIAL PRIMARY KEY,
  email TEXT UNIQUE,          -- one mark per student per session
  device_id TEXT UNIQUE,      -- one mark per device per session
  timestamp DATETIME/TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  form_submitted TEXT,
  ip_address TEXT)
```

- **Same Google account on two phones → second one is rejected.** That is
  the anti-sharing feature, not a bug. Testing with one account will always
  look "broken" on the second device.
- IntegrityError (`sqlite3` or `psycopg`) → friendly page "You or this
  device have already marked attendance" (`main.py` auth_callback +
  finalize_attendance).
- **Replit's Database pane is PostgreSQL-only** — it never lists SQLite
  `.db` files. If attendance is missing there, `DATABASE_URL` isn't set
  (provision the Database tool, then restart the app).

### 2.5 WiFi IP restriction (NAT reality)

The server typically only sees the phone's **public** IP (e.g. `47.29.x`)
while the configured prefix is a **LAN** prefix (`10.x`). So verification is
client-side: `network_check.html` uses WebRTC (`RTCPeerConnection`) to
discover the local IP and POSTs it to `/api/verify-network`, which passes if
*either* local or public IP matches **any** of the comma-separated prefixes
(`10.20.1.,47.29.`). WebRTC may fail (incognito / mDNS masking / cellular) —
then the public fallback decides.

**iOS/iPadOS:** WebKit never exposes the local IP (mDNS), so the page skips
WebRTC entirely (`isIOS`) and the public IP decides. To make geofencing work
on iPhones, `POST /api/admin/start` records the instructor's public IP in
`session_state["admin_public_ip"]`; `/api/verify-network` then also accepts a
student whose public IP equals it (same campus NAT / WiFi). Explicit prefixes
still work and take precedence for both local and public addresses.

`DEFAULT_ALLOWED_IP` (env, default `10.20.1.92`) is **pre-filled** into the
Session Config input (`value="{{ default_ip }}"` on `/admin`); it is fully
editable and clearing the field disables the restriction for that session.
There is deliberately *no* server-side fallback — what you see in the box is
exactly what is enforced.

`get_client_ip()` trusts, in order: `X-Forwarded-For`, `X-Real-IP`,
`CF-Connecting-IP`, `True-Client-IP`, then the socket peer — required behind
Replit/Cloudflare-style proxies.

---

## 3. Change log (everything that was done, newest themes grouped)

### 3.1 UI refresh (all three templates rebuilt)
- `admin_dashboard.html`, `form.html`, `status.html` rewritten: dark
  slate/indigo gradient, glassmorphism cards, Inter + Space Mono,
  indigo `#6366f1` primary, `--card-border: rgba(255,255,255,0.2)`.
- **Admin login modal**: gates the dashboard visually; auth itself is now
  **server-side** (§3.9) — `attemptLogin()` posts to `/api/admin/login` and
  reloads on success. It must NOT call `fetchToken()` directly (that
  endpoint 400s while the session is inactive).
- QR generation happens only after **Start Session** succeeds.
- Email chips (`status.html`, `form.html`): forced single-line /
  centered-wrap treatments — long student emails must not overflow on mobile.
- `form.html`: removed a broken `.glass-placeholder` and an undefined `form`
  variable; `.page { padding-top: 100px }` fixes the mobile heading overlap.

### 3.2 QR sizing
- Sharp square inner QR, **white padding (20px)** on `#qr-container` so the
  finder patterns aren't clipped by rounded corners; container itself gets
  `border-radius`.
- Sizing formula: `max-width: min(640px, 85vw, 70vh)` desktop,
  `min(520px, 85vw, 65vh)` mobile — centered via flex.
- **Do not "improve" this to 760px** — a previous attempt was explicitly
  reverted by the user for making it too big/scroll-inviting.
- Settings panel sits below the QR, `max-width: min(680px, 92vw)`, auto
  side-margins, glass `padding: 36px 40px`.

### 3.3 Form completion flow
- `form_confirmed: set[email]` + `POST /api/form/confirm` +
  `POST /api/form/status` in `main.py`.
- Confirm button hidden until `loadCount >= 2` (two successful iframe loads);
  timer auto-finalizes as `"False (Timeout)"`; `finalized` guard prevents
  double POSTs.

### 3.4 Errors & pages
- `/scan` and `/login` failures return styled `status.html` pages instead of
  raw `HTTPException` JSON.
- `render_status()` helper (module-level) is the standard way to render one.

### 3.5 Multi-device QR scanning — FIXED (see postmortem §5.1)
- `consumed_tokens` changed from `set(nonce)` to
  `dict[(nonce, device_id)] → ts`, 24 h pruning, check keyed per device.
- Verified with a scripted TestClient simulation: two fresh cookie jars scan
  the same token → both pass; each gets blocked only on its own second scan.

### 3.6 Default IP feature
- `DEFAULT_ALLOWED_IP = os.getenv("DEFAULT_ALLOWED_IP", "10.20.1.92")`
  (`main.py`), passed to the template as `default_ip` from the `/admin`
  route, rendered as the input's `value`. Start session uses whatever the
  box contains at submit time.

### 3.7 Load-test bug fixes (found by running §7)
- **`/finalize-attendance` 500 (`no such table: attendance`)**: with
  `session_state["db_file"] == ""` (or after stop), `sqlite3.connect("")`
  silently creates a *temporary empty DB* → INSERT explodes. Now guarded:
  inactive/empty db → friendly "session has been closed" page; also
  `sqlite3.OperationalError` is caught as a fallback. The old
  `try/except/finally` also risked `NameError` if `connect()` itself raised
  (`conn.close()` on an unbound name) — restructured with nested try/finally.
- **`/api/admin/stop` 500 with no session**: now `400 {"detail": ...}` and
  OperationalError on a missing table → 400 instead of 500.

### 3.8 Default timer & default Google form (env pre-fills)
- `DEFAULT_TIMER` (env): pre-fills the **QR Refresh Rate** input. Blank,
  whitespace, or non-numeric → falls back to **45 secs** (guarded with
  `.isdigit()` at config load).
- `DEFAULT_FORM_URL` (env): pre-fills the **Google Form Embed URL** input.
  Blank stays blank — no fallback URL; sessions run with no form.
- `/admin` passes `default_timer` / `default_form_url` into the template
  context (same prefill pattern as `DEFAULT_ALLOWED_IP` §3.6).
- `generate_qr_token`'s `lifespan` default now resolves from
  `DEFAULT_TIMER` when the query param is omitted; an explicit
  `?lifespan=` still wins.
- `toggleTimeout()` runs on page load so a non-blank default form URL
  enables the Form Timeout field immediately (it used to enable only on
  input events).

### 3.9 Admin login moved server-side (default-form-URL leak fix)
- **Problem:** login was client-side only — `attemptLogin()` accepted *any*
  non-empty password, and `GET /admin` shipped the full dashboard HTML
  (including the prefilled default form URL, timer and IP) to anonymous
  visitors.
- **Fix:**
  - `POST /api/admin/login` verifies the password with
    `secrets.compare_digest` and sets `request.session["admin_authed"]`;
    wrong password → `401`.
  - `GET /admin` renders the dashboard **only** when authed — otherwise the
    login screen alone (Jinja `{% if authed %}` around modal and dashboard;
    no `#dashboard`, no prefills in the HTML).
  - `verify_admin_header` → `verify_admin`: accepts the `X-Admin-Password`
    header (scripts / locust) **or** the session cookie.
  - Template JS: `attemptLogin()` posts to the server and
    `location.reload()`s on success; the client-held `adminPassword` was
    removed; dashboard API calls authenticate via the session cookie; any
    `401` mid-use → reload back to the login screen.
- **Verified** with TestClient: anonymous → login screen only (no
  `form-url-input`/`lifespan-input`/IP); wrong password → 401 + still login;
  correct → dashboard with prefills; session-only API call works;
  header-only API call still works (loadtest unaffected).

### 3.10 Admin login expires after 10 minutes
- `POST /api/admin/login` now also stores `admin_authed_at = time.time()`;
  `ADMIN_SESSION_TIMEOUT = 600` (10 min) is a constant next to
  `ADMIN_PASSWORD`.
- `GET /admin` checks the age on every load: older than 10 min → the
  `admin_authed` / `admin_authed_at` session keys are dropped and the login
  screen is rendered (a refresh sends the device back to login).
- Deliberately **page-load only**: API calls keep working while a loaded
  dashboard is open, so the QR auto-refresh does not kick an instructor out
  mid-session (matches the requested "provided that the webpage is
  refreshed").
- Verified with TestClient (time patched): 599 s → dashboard; 601 s → login
  + flags cleared; re-login restores a fresh 10-min window; header API auth
  unaffected.

### 3.11 Replit storage: PostgreSQL when `DATABASE_URL` is set
- **Problem:** on Replit, attendance was invisible in the **Database** pane.
  That pane is Replit-managed **PostgreSQL only** — it never lists SQLite
  files. The app only wrote per-session SQLite, CWD-relative (as was the
  `templates/` path), so on a different CWD the files could even land outside
  the workspace.
- **Fix:**
  - `DATABASE_URL` present (Replit's Database tool provisions it) switches
    storage to PostgreSQL via `psycopg` (3.3.8, added to
    `requirements.txt`). Each session gets its own table
    `attendance_<Y_M_D_H_M>` with `SERIAL` id — browse it in
    **Database → My Data**.
  - No `DATABASE_URL` → unchanged per-session SQLite, now anchored to
    `BASE_DIR = os.path.dirname(os.path.abspath(__file__))`, so the `.db`
    always lands next to `main.py`. `templates/` is anchored the same way.
  - New helpers: `db_connect()` (Postgres connection or SQLite file),
    `session_table()` (quoted PG identifier vs `attendance`), `sql_params()`
    (`?` → `%s`); unified `DB_INTEGRITY_ERROR` / `DB_ERROR`; `session_state`
    gains `db_table` + `session_name`; CSV filename comes from
    `session_name`.
- **Verified:** SQLite flow run from a foreign CWD (DB lands next to
  `main.py`; finalize/stop/CSV/duplicate all OK); Postgres branch exercised
  with a stub driver (CREATE/INSERT/SELECT shapes, `%s` placeholders, quoted
  table, integrity + connection errors → friendly pages / 400). No real
  PostgreSQL server on the dev box.

### 3.12 Deployment vuln pins, logout, iOS geofence, form-embed fallback, default form timeout
- **Dependencies (deployment blocker):** `Authlib` 1.8.0 → **1.9.1** and
  `certifi` 2026.7.22 → **2026.10.18** (PYSEC-2026-4355/4356,
  GHSA-6v38-4h7w-rj9h; PYSEC-2026-4320, GHSA-6q48-6c7w-9gw2).
- **Logout:** `POST /api/admin/logout` pops `admin_authed`/`admin_authed_at`;
  a fixed top-right **Log out** button (authed dashboard only) posts and
  reloads to the login screen. It does **not** stop an active session.
- **iOS WiFi check:** iOS/iPadOS WebKit hides local IPs (mDNS), so
  `network_check.html` skips WebRTC there and the server decides on the
  public IP. `/api/admin/start` records the instructor's public IP
  (`session_state["admin_public_ip"]`) and `/api/verify-network` accepts a
  student whose public IP equals it (same campus NAT) — so the existing LAN
  default keeps working on Android *and* iPhone. The prefix field also
  accepts a **comma-separated list** (`10.20.1.,47.29.`), and the failure
  panel says "not detected (normal on iPhone)" instead of a bare
  "not detected". `/api/check-ip` understands the list too.
- **Google Form embed hang:** some accounts/embeds submit or navigate outside
  the iframe, so the old "confirm appears on the 2nd iframe load" logic never
  fired and the page sat until the timeout. Now the confirm button appears
  after the **first** load (manual confirm), a second load still auto-confirms,
  an **"Open the form in a new tab"** link is always available, and a 6 s
  fallback reveals the confirm button if the embed never loads.
- **Default form timeout:** `DEFAULT_FORM_TIMEOUT` env (blank/non-numeric →
  `300`) is pre-filled in the Session Config and is the server-side fallback
  when `/api/admin/start` receives `timeout <= 0`.
- **Start-failure debugging (added after a Replit report):** `/api/admin/start`
  now returns the real storage error (connection string redacted via
  `safe_db_error()`) instead of a bare 500, the dashboard toast shows the
  status + detail, and `GET /api/admin/diagnostics` (authed) reports
  `storage`, `database_url_set`, `psycopg_installed`, `base_dir`,
  `db_connect_ok` and `db_error` without leaking secrets.
- **Verified:** TestClient 23 checks — login/logout gate, timeout prefill +
  env override (600/blank/junk), timeout-0 fallback, admin-IP capture,
  geofence (local match, iPhone public/admin match, comma prefixes,
  off-network block), template renders; `Authlib 1.9.1` import clean.

---

## 4. File map

| File | Role |
|------|------|
| `main.py` | Everything backend: config (l.16–58), helpers/auth/storage (93–139), admin API incl. login/logout/start/diagnostics (148–309), `/scan` (312–360), network verify (362–401), OAuth (403–455), form APIs (457–474), finalize (477–512) |
| `templates/admin_dashboard.html` | Login gate, QR wrapper, session config, logout button, `detectAdminIP()` |
| `templates/form.html` | Form iframe, timer, confirm button, new-tab fallback |
| `templates/network_check.html` | WebRTC detection (skipped on iOS) + POST `/api/verify-network` |
| `templates/status.html` | Success/error + email chip |
| `loadtest/locustfile.py` | Realistic load test (admins + students; `LOCUST_NO_WAIT=1` for ceiling mode) |
| `requirements.txt` | Pinned deps (generated via `pip freeze`; includes `psycopg` for Replit PostgreSQL) |
| `run.txt` | 2-line run reminder |
| `README.md` | User-facing docs |

---

## 5. Postmortems (so you don't re-litigate them)

### 5.1 "Second phone can't scan — STILL broken after the fix"
**Symptom:** phone A scans fine; phone B (even incognito) errors; reversing
order doesn't help.

**Actual causes found:**
1. **Old code still running** — the fix was correct, the server (Replit
   deployment / a stale uvicorn) hadn't been restarted. Confirmed by the
   user: *"it was using the old code."*
2. **Same Google account on both phones** → `email UNIQUE` rejects the
   second → "You or this device have already marked attendance". By design
   (§2.4).

**Verification that the code itself is correct** (reproduce any time):

```python
# two fresh TestClient cookie jars, same token → both 307 to /login;
# each second scan of the SAME token → "already scanned"
from fastapi.testclient import TestClient; import main
cA, cB = TestClient(main.app), TestClient(main.app)
# (activate main.session_state + mint token first — see git history / §6)
```

**Lesson:** when "the fix didn't work", first confirm *which process* is
serving the traffic.

### 5.2 `/finalize-attendance` → 500 `no such table`
Found **by the load test** (2 failures in the first run). Root cause:
`sqlite3.connect("")` creates a temp DB (§3.7). Fixed + regression-checked
with curl (friendly page, `stop` → 400). Load test then ran **0 errors**
across ~43 000 requests.

### 5.3 Folder rename broke the venv (§1.1) and `pkill` ate the shell (§1.2)

### 5.4 Things tried and rejected
- QR wrapper at `min(760px, …)` — reverted on user request.
- Marking `consumed_tokens` only *after* network verification: considered
  (a failed WiFi check currently burns the key for that QR) but **not
  implemented** — current behavior marks at `/scan` time; keep in mind if
  someone reports "already scanned" after a failed network check.
- Multi-worker uvicorn — blocked by in-memory state (§2.2).

---

## 6. Debugging playbook

```bash
# syntax / import sanity
python3 -m py_compile main.py && venv/bin/python -c "import main; print('ok')"

# is anything listening? (note the bracket trick)
pgrep -af "[u]vicorn"

# simulate the scan logic without a browser
venv/bin/python - <<'EOF'
from fastapi.testclient import TestClient
import main, sqlite3, time
main.session_state.update({"is_active": True, "form_url": "", "timeout": 300,
                           "allowed_ip": "", "db_file": "scratch.db"})
sqlite3.connect("scratch.db").execute(
  "CREATE TABLE IF NOT EXISTS attendance (id INTEGER PRIMARY KEY, email TEXT UNIQUE,"
  " device_id TEXT UNIQUE, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,"
  " form_submitted TEXT, ip_address TEXT)")
tok = main.serializer.dumps({"nonce": str(time.time()), "lifespan": 45})
for name in ("A", "B"):
    c = TestClient(main.app)
    r = c.get(f"/scan?token={tok}", follow_redirects=False)
    print(name, r.status_code, r.headers.get("location"))
EOF

# end-to-end manual: /admin → password → Start → scan with phone
# (QR only appears AFTER start succeeds — do not expect one on page load)

# admin login gate regression check
venv/bin/python - <<'EOF'
from fastapi.testclient import TestClient
import main
c = TestClient(main.app)
assert 'id="dashboard"' not in c.get("/admin").text          # anonymous: login only
assert c.post("/api/admin/login", json={"password": "x"}).status_code == 401
assert c.post("/api/admin/login", json={"password": main.ADMIN_PASSWORD}).status_code == 200
assert 'id="dashboard"' in c.get("/admin").text              # authed: dashboard + prefills
EOF
```

- Server errors land in the uvicorn log; for tracebacks run without `--reload`
  and grep `Traceback`.
- Cookies: `device_session` (5 min) vs `device_id` (1 yr) — "Device not
  verified" means the 5-minute one is missing/expired → scan again.

---

## 7. Load test — methodology & findings (2026-11-01)

**Setup:** 2 vCPU / 4 GB dev box; single `uvicorn main:app` worker; locust
drive from the *same* machine; realistic mix (admins refresh QR +
students burst-scan the same QR + finalize writes), Google round-trip out of
scope (stops at the 302 to accounts.google.com).

**Results:**

| Run | Users | Think-time | RPS | p50 | p95 | Errors |
|-----|------:|------------|----:|----:|----:|-------:|
| 1 | 50 | yes | 67 | 9 ms | 27 ms | 0 |
| 2 | 200 | yes | 243 | 25 ms | 320 ms | 0 |
| 3 | 500 | yes | 253 | 1.0 s | 2.3 s | 0 |
| 4 | 300 | **none** | 257 | 1.0 s | 2.2 s | 0 |
| 5 | 50 | **none** | 258 | 160 ms | 430 ms | 0 |
| probe | 1 (sequential) | — | — | **1.7 ms** | 2.4 ms | 0 |

Also observed: uvicorn CPU **~46 %** (i.e. ~1.8 ms of event-loop time per
request) and RSS ~1 GB under sustained load. Total: ~43 000 requests,
**0 errors after the §3.7 fix**.

**Interpretation:**
- Sequential 1.7 ms/req ⇒ the single-threaded event loop tops out around
  **500–600 RPS** when it alone is the constraint.
- Runs 4 & 5 plateau at the *same* ~258 RPS regardless of user count while
  server CPU stays < 50 % ⇒ the **locust client (same 2 cores)** is the
  measured bottleneck, not the app.
- Latency explosion at 500 users (p50 1 s) = queueing behind the loop +
  client CPU contention — throughput stops growing, requests just wait.
- **Real classroom demand:** ~300 students × a handful of requests inside a
  45 s window ≈ 10–20 RPS peak → the app runs at **< 5 % of measured
  capacity**; a full class finishes in any of these runs in seconds.

**If actual scale ever matters (priority order):**
1. Move blocking `sqlite3` off the event loop
   (`asyncio.to_thread` / `aiosqlite`) — biggest single win.
2. Disable uvicorn access logs in prod (`--no-access-log`) — per-request
   synchronous logging costs loop time.
3. Move `session_state` / `consumed_tokens` / `form_confirmed` to SQLite or
   Redis → then `--workers N` becomes possible (currently forbidden, §2.2).
4. WAL mode + one long-lived connection instead of connect/commit/close per
   request (each commit is an fsync).

Reproduce:

```bash
/home/kshitij/Documents/Code/attendance-app/venv/bin/python -m locust \
  -f loadtest/locustfile.py --headless -u 50 -r 10 -t 45s \
  --host http://127.0.0.1:8000 --only-summary
LOCUST_NO_WAIT=1 ... # same, ceiling mode
```

---

## 8. Known debt / good next tasks

1. **`SECRET_KEY`, `ADMIN_PASSWORD` defaults are hardcoded in source** —
   secrets should be env-only in production (README warns).
2. **Burned-key edge:** a student who fails the WiFi check has their
   `(nonce, device_id)` consumed at `/scan` time — retrying the *same* QR in
   its 45 s window says "already scanned". Move the mark to after successful
   verification if this is ever reported (needs `nonce` added to `net_token`).
3. **`consumed_tokens` grows for 24 h** — fine now (pruned), but if state
   moves to a store, keep the TTL.
4. No automated tests — the §6 simulation is the seed for a real suite.
5. `form.html` confirm UX still watches iframe load events for auto-confirm;
   the manual confirm + new-tab link + 6 s fallback (§3.12) make it robust,
   but Google UI changes can always alter the auto-detect.
6. Attendance CSV column "Device Fingerprint" exposes raw `device_id` —
   intentional for auditing; don't log it anywhere public.
7. **No login rate-limiting** — explicit logout now exists (§3.12) and auth
   expires 10 min after login (§3.10, page-load check only), but
   `/api/admin/login` has no lockout (fine for a local tool; add a backoff if
   shared machines ever matter).

---

*When in doubt: single worker, restart after edits, one Google account per
test device, and the QR wrapper stays at `min(640px, 85vw, 70vh)`.*
