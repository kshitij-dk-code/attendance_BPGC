from fastapi import FastAPI, Request, HTTPException, Cookie, Depends, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from itsdangerous import URLSafeTimedSerializer
from authlib.integrations.starlette_client import OAuth
from pydantic import BaseModel
from datetime import datetime
import sqlite3
import time
import os
import secrets
import io
import asyncio
import csv
import json
import re
import traceback
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx

os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"

app = FastAPI()
SECRET_KEY = "your-super-secret-key"
ALLOWED_EMAIL_DOMAIN = "@goa.bits-pilani.ac.in"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
# Admin dashboard login is valid for this long after the password is entered.
# Enforced whenever /admin is loaded (i.e. on page refresh).
ADMIN_SESSION_TIMEOUT = 600  # 10 minutes
# Default WiFi IP prefix pre-filled in the Session Config (still editable per session).
# Set DEFAULT_ALLOWED_IP env var to override, or set it to "" to ship with no default.
DEFAULT_ALLOWED_IP = os.getenv("DEFAULT_ALLOWED_IP", "")
# Default QR refresh timer (seconds) pre-filled in the Session Config.
# Leave it blank to fall back to 45 secs (non-numeric values also fall back).
DEFAULT_TIMER = os.getenv("DEFAULT_TIMER", "15").strip() or "45"
if not DEFAULT_TIMER.isdigit():
    DEFAULT_TIMER = "45"
# Default Google Form embed URL pre-filled in the Session Config.
# Leave it blank and it stays blank (sessions then run with no form).
DEFAULT_FORM_URL = os.getenv("DEFAULT_FORM_URL", "https://forms.gle/oVFgrVjKUjwkqaie9").strip()
# Default form timeout (seconds) pre-filled in the Session Config.
# Leave it blank to fall back to 300 secs (non-numeric values also fall back).
DEFAULT_FORM_TIMEOUT = os.getenv("DEFAULT_FORM_TIMEOUT", "300").strip() or "300"
if not DEFAULT_FORM_TIMEOUT.isdigit():
    DEFAULT_FORM_TIMEOUT = "300"

# Storage: Replit sets DATABASE_URL for its managed PostgreSQL (Database tool).
# When present, attendance is written there — visible in Replit's Database pane
# and persistent across restarts. Otherwise (local dev) it falls back to
# per-session SQLite files in this file's directory.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USE_POSTGRES = bool(DATABASE_URL)
PSYCOPG_INSTALLED = False
if USE_POSTGRES:
    try:
        import psycopg
        PSYCOPG_INSTALLED = True
    except ImportError:
        USE_POSTGRES = False
        print("WARNING: DATABASE_URL is set but psycopg is not installed — falling back to SQLite.")

DB_INTEGRITY_ERROR = psycopg.IntegrityError if USE_POSTGRES else sqlite3.IntegrityError
DB_ERROR = psycopg.Error if USE_POSTGRES else sqlite3.Error

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
serializer = URLSafeTimedSerializer(SECRET_KEY)
consumed_tokens = {}  # (nonce, device_id) -> scanned_at timestamp; QR can be scanned by many DIFFERENT devices
CONSUMED_MAX_AGE = 86400

# Tracks the dynamic DB file for the current session
session_state = {
    "is_active": False,
    "form_url": "",
    "timeout": int(DEFAULT_FORM_TIMEOUT),
    "allowed_ip": "",
    "admin_public_ip": "",
    "form_email_entry": "",
    "form_signin_required": False,
    "form_view_url": "",
    "quiz": None,
    "native_reason": "",
    "db_file": "",
    "db_table": "",
    "session_name": ""
}

oauth = OAuth()
oauth.register(
    name='google',
    client_id=os.getenv("YOUR_GOOGLE_CLIENT_ID", "google_client_id_here"),
    client_secret=os.getenv("YOUR_GOOGLE_CLIENT_SECRET", "google_client_secret_here"),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile', 'prompt': 'select_account'}
)

TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
if not os.path.exists(TEMPLATES_DIR):
    os.makedirs(TEMPLATES_DIR)
templates = Jinja2Templates(directory=TEMPLATES_DIR)

def render_status(request, msg, success=False, email=None):
    return templates.TemplateResponse(request=request, name="status.html", context={"message": msg, "success": success, "email": email})

def verify_admin(request: Request):
    # Accept the X-Admin-Password header (scripts / load test) OR the
    # authenticated admin session cookie issued by POST /api/admin/login.
    auth_header = request.headers.get("X-Admin-Password")
    if auth_header and secrets.compare_digest(auth_header, ADMIN_PASSWORD):
        return True
    if request.session.get("admin_authed"):
        return True
    raise HTTPException(status_code=401, detail="Unauthorized")

def safe_db_error(exc):
    """Short DB error text with the connection string (password!) redacted."""
    msg = f"{type(exc).__name__}: {exc}"
    if DATABASE_URL:
        msg = msg.replace(DATABASE_URL, "***")
    return msg[:500]

# Google Forms question type codes (reverse engineered from the public blob).
# 0 short, 1 paragraph, 2 radio, 3 dropdown, 4 checkboxes, 5 linear scale.
FORM_QUESTION_TYPES = {0: "short", 1: "paragraph", 2: "radio", 3: "dropdown", 4: "checkbox", 5: "scale"}

def parse_form_structure(html):
    """Parse Google Forms' FB_PUBLIC_LOAD_DATA_ blob into a renderable quiz.

    Returns a dict with title/description/items, the email entry (if any),
    and flags that make native mode unsafe (multi-page, unsupported types,
    "Other" options).
    """
    result = {
        "ok": False, "title": "", "description": "", "items": [],
        "email_entry": "", "multi_page": False, "unsupported_types": [],
        "has_other": False,
    }
    marker = "FB_PUBLIC_LOAD_DATA_"
    idx = html.find(marker)
    if idx == -1:
        return result
    start = html.find("[", idx)
    if start == -1:
        return result
    try:
        data, _ = json.JSONDecoder().raw_decode(html, start)
        meta = data[1]
        items_raw = meta[1] or []
        result["title"] = meta[8] if len(meta) > 8 and isinstance(meta[8], str) else ""
        result["description"] = meta[0] if meta and isinstance(meta[0], str) else ""
    except (ValueError, IndexError, TypeError):
        return result
    result["ok"] = True

    for raw in items_raw:
        if not isinstance(raw, list) or len(raw) < 5:
            continue
        qtype = raw[3] if isinstance(raw[3], int) else None
        title = raw[1] if isinstance(raw[1], str) else ""
        desc = raw[2] if len(raw) > 2 and isinstance(raw[2], str) else ""
        if qtype == 6:       # section title / description block
            continue
        if qtype == 8:       # page break
            result["multi_page"] = True
            continue
        if qtype in (11, 12):  # image / video
            continue
        if qtype not in FORM_QUESTION_TYPES:
            result["unsupported_types"].append(qtype if qtype is not None else -1)
            continue
        entries = raw[4] if isinstance(raw[4], list) else []
        if not entries:
            continue
        sub = entries[0]
        if not isinstance(sub, list) or not sub:
            continue
        entry_id = sub[0] if isinstance(sub[0], int) else None
        options = []
        if len(sub) > 1 and isinstance(sub[1], list):
            for opt in sub[1]:
                if isinstance(opt, list) and opt and isinstance(opt[0], str):
                    options.append(opt[0])
                    if opt[0].strip().lower() == "other":
                        result["has_other"] = True
        required = bool(sub[2]) if len(sub) > 2 else False
        if entry_id is None:
            continue
        result["items"].append({
            "entry": str(entry_id), "title": title or "Question",
            "description": desc, "type": qtype,
            "options": options, "required": required, "is_email": False,
        })

    for item in result["items"]:
        if not result["email_entry"] and item["type"] in (0, 1) and re.search(r"e-?mail", item["title"], re.IGNORECASE):
            result["email_entry"] = item["entry"]
            item["is_email"] = True
    return result

def parse_form_email_entry(html):
    """Find the entry ID of the first short-answer/paragraph email question."""
    return parse_form_structure(html)["email_entry"]

def derive_form_response_url(view_url):
    """Turn a /viewform URL (or forms.gle result) into /formResponse."""
    match = re.search(r"https://docs\.google\.com/forms/(?:u/\d+/)?d/e/([^/?#]+)", view_url)
    if match:
        return f"https://docs.google.com/forms/d/e/{match.group(1)}/formResponse"
    if "/viewform" in view_url:
        return view_url.replace("/viewform", "/formResponse")
    return view_url.rstrip("/") + "/formResponse"

def native_suitability(form, signin_required):
    """Can this form be rendered natively (email hidden + filled server-side)?"""
    if signin_required:
        return False, "Form requires Google sign-in — turn off 'Collect email addresses' and 'Limit to 1 response'."
    if not form or not form.get("ok"):
        return False, "Could not read the form structure."
    if form.get("multi_page"):
        return False, "Multi-page forms are not supported in native mode."
    if form.get("unsupported_types"):
        types = sorted({t for t in form["unsupported_types"] if t != -1})
        return False, f"Unsupported question types: {types}."
    if form.get("has_other"):
        return False, "Forms with an 'Other' option are not supported in native mode."
    if not form.get("email_entry"):
        return False, "Add a short-answer Email question (mark it required) so responses carry the verified email."
    return True, ""

def inspect_form(form_url):
    """Fetch a Google Form and return its structure + native-mode verdict."""
    result = {
        "email_entry": "", "signin_required": False, "view_url": form_url,
        "response_url": "", "quiz": None, "native_ok": False,
        "native_reason": "No form URL.",
    }
    if not form_url:
        return result
    try:
        resp = httpx.get(
            form_url,
            timeout=10.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; attendance-app)"},
        )
    except Exception:
        result["native_reason"] = "Could not fetch the form (network error)."
        return result

    result["view_url"] = str(resp.url)
    if "accounts.google.com" in str(resp.url):
        result["signin_required"] = True
        result["native_reason"] = "Form requires Google sign-in."
        return result
    if resp.status_code != 200:
        result["native_reason"] = f"Could not fetch the form (HTTP {resp.status_code})."
        return result

    # Public forms show a "Sign in to Google" footer link — only the real
    # login page (identifier field) means the form itself requires auth.
    result["signin_required"] = 'id="identifierId"' in resp.text
    form = parse_form_structure(resp.text)
    result["quiz"] = form
    result["email_entry"] = form["email_entry"]
    result["response_url"] = derive_form_response_url(result["view_url"])
    result["native_ok"], result["native_reason"] = native_suitability(form, result["signin_required"])
    return result

def build_form_response_data(form, answers, email):
    """Build the entry.* tuples Google expects (email always server-side)."""
    data = []
    for item in form["items"]:
        entry = item["entry"]
        if item.get("is_email"):
            values = [email]
        else:
            value = answers.get(entry)
            if value in (None, "", []):
                continue
            if item["type"] == 4:
                values = value if isinstance(value, list) else [value]
            else:
                values = value if isinstance(value, list) else [value]
        for value in values:
            data.append((f"entry.{entry}", str(value)))
    data.extend([("fvv", "1"), ("pageHistory", "0")])
    return data

def submit_to_google(view_url, payload):
    """POST answers to the public Google Form endpoint.

    Returns (ok, detail). Google's endpoint is undocumented; a 200 without
    error markers is treated as success, but the explicit "recorded" page is
    preferred. Caller must not mark attendance when ok is False.
    """
    response_url = derive_form_response_url(view_url)
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36",
        "Referer": view_url,
    }
    try:
        with httpx.Client(follow_redirects=True, timeout=15.0, headers=headers) as client:
            page = client.get(view_url)
            if page.status_code != 200:
                return False, f"could not load the form (HTTP {page.status_code})"
            fbzx = re.search(r'name="fbzx" value="([^"]+)"', page.text)
            body = list(payload)
            if fbzx:
                body.append(("fbzx", fbzx.group(1)))
            body.append(("submit", "Submit"))
            resp = client.post(
                response_url,
                content=urlencode(body),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            text = resp.text.lower()
            if resp.status_code >= 400:
                if "required question" in text:
                    return False, "Google rejected the response: a required question is missing"
                if "no longer accepting" in text:
                    return False, "the form is no longer accepting responses"
                if "sign in" in text:
                    return False, "the form requires Google sign-in"
                return False, f"Google rejected the response (HTTP {resp.status_code})"
            if "accounts.google.com" in str(resp.url) or 'id="identifierid"' in text:
                return False, "the form requires Google sign-in"
            if "required question" in text or "data-validation-failed" in text:
                return False, "Google rejected the response: a required question is missing"
            if "no longer accepting" in text:
                return False, "the form is no longer accepting responses"
            if "recorded" in text or "submitted" in text or "thanks" in text:
                return True, ""
            # Localized success pages may not contain the English marker.
            print(f"Google form submit: HTTP {resp.status_code} without a success marker")
            return True, ""
    except Exception as exc:
        return False, safe_db_error(exc)

def build_form_url(url, email, email_entry=""):
    """Bind the form to the OAuth-verified email.

    - Replaces a `{email}` placeholder.
    - Prefills the detected email question (`entry.<id>`) with the email.
    - Adds/replaces `authuser=<email>` so Google opens the form with that
      account selected when it is signed in to the browser.
    """
    encoded = quote(email, safe="")
    url = url.replace("{email}", encoded)
    parts = urlsplit(url)
    params = [p for p in parts.query.split("&") if p and p.split("=", 1)[0].lower() != "authuser"]
    if email_entry:
        entry_key = f"entry.{email_entry}"
        params = [p for p in params if p.split("=", 1)[0] != entry_key]
        params.append(f"{entry_key}={encoded}")
    params.append(f"authuser={encoded}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(params), parts.fragment))

def get_client_ip(request: Request):
    # Try multiple headers in order of reliability behind proxies
    for header in ["X-Forwarded-For", "X-Real-IP", "CF-Connecting-IP", "True-Client-IP"]:
        val = request.headers.get(header)
        if val:
            ip = val.split(",")[0].strip()
            if ip and ip != "unknown":
                return ip
    return request.client.host if request.client else "unknown"

def db_connect():
    """Open the current session's storage (PostgreSQL or the session's SQLite file)."""
    if USE_POSTGRES:
        return psycopg.connect(DATABASE_URL)
    return sqlite3.connect(session_state["db_file"])

def session_table():
    """SQL identifier of the table holding the current session's rows."""
    if USE_POSTGRES:
        return f'"{session_state["db_table"]}"'
    return "attendance"

def sql_params(query):
    """Translate qmark placeholders to the active driver's style."""
    return query.replace("?", "%s") if USE_POSTGRES else query

class StartSessionRequest(BaseModel):
    form_url: str
    timeout: int
    allowed_ip: str

class LoginRequest(BaseModel):
    password: str

class InspectFormRequest(BaseModel):
    form_url: str

class QuizSubmitRequest(BaseModel):
    token: str
    answers: dict

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    # The dashboard HTML (with its prefilled default form URL / timer / IP) is
    # only rendered for an authenticated admin session — everyone else gets
    # the login screen alone.
    authed = bool(request.session.get("admin_authed"))
    if authed and time.time() - request.session.get("admin_authed_at", 0) > ADMIN_SESSION_TIMEOUT:
        # Login is older than 10 minutes — drop it and show the login screen.
        request.session.pop("admin_authed", None)
        request.session.pop("admin_authed_at", None)
        authed = False
    context = {"authed": authed}
    if authed:
        context.update({
            "default_ip": DEFAULT_ALLOWED_IP,
            "default_timer": DEFAULT_TIMER,
            "default_form_url": DEFAULT_FORM_URL,
            "default_form_timeout": DEFAULT_FORM_TIMEOUT,
        })
    return templates.TemplateResponse(request=request, name="admin_dashboard.html", context=context)

@app.post("/api/admin/login")
async def admin_login(req: LoginRequest, request: Request):
    if not secrets.compare_digest(req.password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="Incorrect password")
    request.session["admin_authed"] = True
    request.session["admin_authed_at"] = time.time()
    return {"status": "ok"}

@app.post("/api/admin/logout")
async def admin_logout(request: Request):
    request.session.pop("admin_authed", None)
    request.session.pop("admin_authed_at", None)
    return {"status": "logged_out"}

@app.post("/api/admin/start")
async def start_session(req: StartSessionRequest, request: Request, authorized: bool = Depends(verify_admin)):
    # Create a brand new storage target for this specific session: a Postgres
    # table on Replit, or a SQLite file next to main.py locally.
    stamp = datetime.now().strftime('%Y_%m_%d_%H_%M')

    session_state["is_active"] = True
    session_state["form_url"] = req.form_url.strip()
    session_state["timeout"] = req.timeout if req.timeout and req.timeout > 0 else int(DEFAULT_FORM_TIMEOUT)
    session_state["allowed_ip"] = req.allowed_ip.strip()
    session_state["admin_public_ip"] = get_client_ip(request)
    details = await asyncio.to_thread(inspect_form, session_state["form_url"])
    if details.get("view_url"):
        session_state["form_url"] = details["view_url"]
    session_state["form_view_url"] = details.get("view_url", session_state["form_url"])
    session_state["form_email_entry"] = details.get("email_entry", "")
    session_state["form_signin_required"] = details.get("signin_required", False)
    session_state["quiz"] = details.get("quiz") if details.get("native_ok") else None
    session_state["native_reason"] = details.get("native_reason", "")
    session_state["session_name"] = f"Attendance_{stamp}"

    if USE_POSTGRES:
        session_state["db_file"] = ""
        session_state["db_table"] = f"attendance_{stamp}"
        create_sql = f'''CREATE TABLE IF NOT EXISTS {session_table()}
                 (id SERIAL PRIMARY KEY,
                  email TEXT UNIQUE,
                  device_id TEXT UNIQUE,
                  timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  form_submitted TEXT,
                  ip_address TEXT,
                  quiz_answers TEXT)'''
    else:
        session_state["db_table"] = ""
        session_state["db_file"] = os.path.join(BASE_DIR, f"Attendance_{stamp}.db")
        create_sql = '''CREATE TABLE IF NOT EXISTS attendance
                 (id INTEGER PRIMARY KEY,
                  email TEXT UNIQUE,
                  device_id TEXT UNIQUE,
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                  form_submitted TEXT,
                  ip_address TEXT,
                  quiz_answers TEXT)'''

    try:
        conn = db_connect()
        try:
            conn.execute(create_sql)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        traceback.print_exc()
        session_state["is_active"] = False
        storage = "PostgreSQL" if USE_POSTGRES else "SQLite"
        raise HTTPException(status_code=500, detail=f"Could not initialize {storage} storage: {safe_db_error(exc)}")

    quiz = session_state["quiz"]
    return {"status": "started", "storage": "postgres" if USE_POSTGRES else "sqlite",
            "db_file": session_state["db_file"], "db_table": session_state["db_table"],
            "form_email_entry": session_state["form_email_entry"],
            "form_signin_required": session_state["form_signin_required"],
            "native_quiz": bool(quiz),
            "question_count": len([i for i in quiz["items"] if not i.get("is_email")]) if quiz else 0,
            "native_reason": session_state["native_reason"]}

@app.post("/api/admin/inspect-form")
async def admin_inspect_form(req: InspectFormRequest, authorized: bool = Depends(verify_admin)):
    """Check a form before starting: native mode possible? email/sign-in?"""
    url = req.form_url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Form URL must start with http:// or https://")
    details = await asyncio.to_thread(inspect_form, url)
    quiz = details.get("quiz") or {}
    return {
        "email_entry": details.get("email_entry", ""),
        "signin_required": details.get("signin_required", False),
        "native_ok": details.get("native_ok", False),
        "native_reason": details.get("native_reason", ""),
        "title": quiz.get("title", ""),
        "question_count": len([i for i in quiz.get("items", []) if not i.get("is_email")]),
    }

@app.get("/api/admin/diagnostics")
async def admin_diagnostics(authorized: bool = Depends(verify_admin)):
    """Read-only storage diagnostics (no secrets) — used to debug Replit."""
    info = {
        "storage": "postgres" if USE_POSTGRES else "sqlite",
        "database_url_set": bool(DATABASE_URL),
        "psycopg_installed": PSYCOPG_INSTALLED,
        "base_dir": BASE_DIR,
        "session_active": session_state["is_active"],
        "db_connect_ok": None,
    }
    if USE_POSTGRES:
        try:
            conn = psycopg.connect(DATABASE_URL)
            try:
                conn.execute("SELECT 1")
            finally:
                conn.close()
            info["db_connect_ok"] = True
        except Exception as exc:
            info["db_connect_ok"] = False
            info["db_error"] = safe_db_error(exc)
    return info

@app.post("/api/admin/stop")
async def stop_session(authorized: bool = Depends(verify_admin)):
    session_state["is_active"] = False

    if not (session_state["db_file"] or session_state["db_table"]):
        raise HTTPException(status_code=400, detail="No session has been started yet")

    # Extract data from the session's specific storage target
    try:
        conn = db_connect()
        try:
            c = conn.cursor()
            c.execute(f"SELECT id, email, device_id, timestamp, form_submitted, ip_address, quiz_answers FROM {session_table()}")
            rows = c.fetchall()
        finally:
            conn.close()
    except DB_ERROR:
        raise HTTPException(status_code=400, detail="Session database is not initialized")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Email", "Device Fingerprint", "Timestamp (UTC)", "Form Submitted", "IP Address", "Quiz Answers"])
    for row in rows:
        writer.writerow(row)
    output.seek(0)

    filename = f"{session_state.get('session_name') or 'Attendance'}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.get("/api/admin/generate-qr-token")
async def generate_qr_token(request: Request, lifespan: int = None, authorized: bool = Depends(verify_admin)):
    if lifespan is None:
        lifespan = int(DEFAULT_TIMER)  # blank default timer falls back to 45
    if not session_state["is_active"]:
        raise HTTPException(status_code=400, detail="Session not active")
        
    nonce = str(time.time())
    token = serializer.dumps({"nonce": nonce, "lifespan": lifespan})
    return {"token": token, "scan_url": f"{str(request.base_url).rstrip('/')}/scan?token={token}", "expires_in": lifespan}

@app.get("/api/check-ip")
async def check_ip(request: Request):
    client_ip = get_client_ip(request)
    prefixes = [p.strip() for p in (session_state.get("allowed_ip") or "").split(",") if p.strip()]
    return {
        "detected_ip": client_ip,
        "x_forwarded_for": request.headers.get("X-Forwarded-For", ""),
        "x_real_ip": request.headers.get("X-Real-IP", ""),
        "raw_client_host": request.client.host if request.client else "",
        "allowed_prefix": session_state.get("allowed_ip", ""),
        "wifi_match": any(client_ip.startswith(p) for p in prefixes) if prefixes else None
    }

@app.get("/scan")
async def scan_qr(request: Request, token: str):
    if not session_state["is_active"]:
        return render_status(request, "The attendance session is closed. Please ask your instructor to start a new session.")

    client_ip = get_client_ip(request)
    allowed_ip = (session_state.get("allowed_ip") or "").strip()

    # Validate the QR token
    try:
        data = serializer.loads(token, max_age=86400)
        if time.time() - float(data["nonce"]) > int(data.get("lifespan", 45)):
            raise Exception("Expired")
    except Exception:
        return render_status(request, "QR Code expired. Please scan the newest QR code shown on screen.")

    # Lock the Device Fingerprint
    device_id = request.cookies.get("device_id")
    if not device_id:
        device_id = secrets.token_hex(16)

    # Only block the SAME device from reusing a QR code — other students
    # scanning the same QR with their own devices must be allowed through.
    now = time.time()
    for key in [k for k, ts in consumed_tokens.items() if now - ts > CONSUMED_MAX_AGE]:
        del consumed_tokens[key]

    scan_key = (data["nonce"], device_id)
    if scan_key in consumed_tokens:
        return render_status(request, "You have already scanned this QR code. Please scan the newest one if it refreshed.")
    consumed_tokens[scan_key] = now

    # WiFi restriction: verify the phone's LOCAL IP (client-side, since the
    # server only sees the public/NAT'd address) before issuing cookies.
    if allowed_ip:
        net_token = serializer.dumps({"device_id": device_id, "ip": client_ip, "purpose": "network_check"})
        return templates.TemplateResponse(request=request, name="network_check.html", context={
            "net_token": net_token,
            "allowed_prefix": allowed_ip
        })

    device_session = serializer.dumps({"scanned_at": time.time(), "ip": client_ip, "device_id": device_id})
    response = RedirectResponse(url="/login")
    response.set_cookie(key="device_session", value=device_session, httponly=True, max_age=300)

    # Make the device fingerprint permanent so they cannot pass the phone to a friend
    response.set_cookie(key="device_id", value=device_id, httponly=True, max_age=31536000)
    return response

@app.post("/api/verify-network")
async def verify_network(net_token: str = Form(...), local_ip: str = Form("")):
    """Verify the phone's local WiFi IP (detected via WebRTC) against the
    allowed prefix(es). Falls back to the public IP the server observed, or
    the instructor's public IP (covers iOS, which hides local IPs)."""
    if not session_state["is_active"]:
        return JSONResponse({"ok": False, "message": "The attendance session has been closed."})

    try:
        data = serializer.loads(net_token, max_age=300)
        device_id = data["device_id"]
        public_ip = data["ip"]
    except Exception:
        return JSONResponse({"ok": False, "message": "Network check expired. Please scan the QR code again."})

    # Prefixes may be a comma-separated list (e.g. a LAN prefix plus the
    # campus public-IP prefix).
    prefixes = [p.strip() for p in (session_state.get("allowed_ip") or "").split(",") if p.strip()]
    if prefixes:
        local_ip = (local_ip or "").strip()
        local_ok = bool(local_ip) and any(local_ip.startswith(p) for p in prefixes)
        public_ok = any(public_ip.startswith(p) for p in prefixes)
        # iPhones (WebKit) never expose the local WiFi IP, so also accept
        # students sharing the instructor's public IP (same NAT/campus WiFi).
        admin_public_ip = (session_state.get("admin_public_ip") or "").strip()
        admin_ok = bool(admin_public_ip) and public_ip == admin_public_ip
        if not (local_ok or public_ok or admin_ok):
            return JSONResponse({
                "ok": False,
                "message": "You must be connected to the required WiFi network to mark attendance.",
                "expected": ", ".join(prefixes),
                "local_ip": local_ip or "not detected (normal on iPhone)",
                "public_ip": public_ip
            })

    # Network verified — issue the device cookies
    device_session = serializer.dumps({"scanned_at": time.time(), "ip": public_ip, "device_id": device_id})
    resp = JSONResponse({"ok": True, "redirect": "/login"})
    resp.set_cookie(key="device_session", value=device_session, httponly=True, max_age=300)
    resp.set_cookie(key="device_id", value=device_id, httponly=True, max_age=31536000)
    return resp

@app.get("/login")
async def login(request: Request, device_session: str = Cookie(None)):
    if not device_session:
        return render_status(request, "Device not verified. Please scan the QR code again.")
    return await oauth.google.authorize_redirect(request, str(request.url_for('auth_callback')))

@app.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback(request: Request, device_session: str = Cookie(None)):
    def render_status(msg, success=False, email=None):
        return templates.TemplateResponse(request=request, name="status.html", context={"message": msg, "success": success, "email": email})

    if not device_session or not session_state["is_active"]:
        return render_status("Invalid or closed session.", success=False)
    
    try:
        session_data = serializer.loads(device_session, max_age=300)
        device_id = session_data["device_id"]
        
        token = await oauth.google.authorize_access_token(request)
        email = token.get('userinfo').get('email')
    except Exception:
        return render_status("Authentication failed or took too long.", success=False)
    
    if not email.endswith(ALLOWED_EMAIL_DOMAIN):
        return render_status(f"Use your {ALLOWED_EMAIL_DOMAIN} account.", success=False)
    
    # Server-side backstop: the form may be opened in another window, so a
    # late "submitted" signal must not confirm attendance after the timer.
    deadline = time.time() + session_state["timeout"] + 15
    form_token = serializer.dumps({"email": email, "device_id": device_id, "deadline": deadline})
    
    # If NO form is required, insert directly
    if not session_state["form_url"]:
        try:
            conn = db_connect()
            try:
                conn.execute(sql_params(f"INSERT INTO {session_table()} (email, device_id, form_submitted, ip_address) VALUES (?, ?, ?, ?)"),
                             (email, device_id, "N/A (No Form)", get_client_ip(request)))
                conn.commit()
            finally:
                conn.close()
        except DB_INTEGRITY_ERROR:
            return render_status("Error: You or this device have already marked attendance today.", success=False, email=email)
        except DB_ERROR:
            return render_status("The attendance session has been closed.", success=False, email=email)
        return render_status("Attendance Marked Successfully!", success=True, email=email)

    quiz = session_state.get("quiz")
    if quiz:
        return templates.TemplateResponse(request=request, name="quiz.html", context={
            "email": email,
            "form_token": form_token,
            "quiz": quiz,
            "questions": [i for i in quiz["items"] if not i.get("is_email")],
            "timeout_seconds": session_state["timeout"],
        })

    return templates.TemplateResponse(request=request, name="form.html", context={
        "email": email, 
        "form_token": form_token, 
        "form_url": build_form_url(session_state["form_url"], email, session_state.get("form_email_entry", "")),
        "timeout_seconds": session_state["timeout"],
        "signin_required": session_state.get("form_signin_required", False),
        "email_prefilled": bool(session_state.get("form_email_entry")),
    })

# Track which form tokens have been confirmed as submitted
form_confirmed = set()

@app.post("/api/form/confirm")
async def confirm_form(token: str = Form(...)):
    try:
        data = serializer.loads(token, max_age=1800)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    deadline = data.get("deadline")
    if deadline is not None and time.time() > deadline:
        raise HTTPException(status_code=400, detail="Form time expired")
    form_confirmed.add(data["email"])
    return {"status": "confirmed"}

@app.post("/api/form/status")
async def form_status(token: str = Form(...)):
    try:
        data = serializer.loads(token, max_age=1800)
        confirmed = data["email"] in form_confirmed
        return {"confirmed": confirmed}
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or expired token")

def normalize_quiz_answers(quiz, raw):
    """Validate answers against the parsed form; email is never client-supplied."""
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Invalid quiz answers")
    cleaned = {}
    for item in quiz["items"]:
        entry = item["entry"]
        if item.get("is_email"):
            continue
        qtype = item["type"]
        value = raw.get(entry)
        if qtype in (0, 1):
            if not isinstance(value, str):
                value = ""
            value = value.strip()
            if len(value) > 5000:
                raise HTTPException(status_code=400, detail=f"Answer too long: {item['title']}")
        elif qtype in (2, 3, 5):
            if not isinstance(value, str):
                value = ""
            if value and value not in item["options"]:
                raise HTTPException(status_code=400, detail=f"Invalid choice for: {item['title']}")
        elif qtype == 4:
            if value is None:
                value = []
            if not isinstance(value, list):
                raise HTTPException(status_code=400, detail=f"Invalid choices for: {item['title']}")
            value = [v for v in value if isinstance(v, str)]
            if any(v not in item["options"] for v in value):
                raise HTTPException(status_code=400, detail=f"Invalid choice for: {item['title']}")
            value = list(dict.fromkeys(value))
        else:
            continue
        if item["required"] and not value:
            raise HTTPException(status_code=400, detail=f"Please answer: {item['title']}")
        if value not in ("", [], None):
            cleaned[entry] = value
    return cleaned

def quiz_answers_for_csv(quiz, answers, email):
    row = {"Form": quiz.get("title", ""), "Email (verified)": email}
    for item in quiz["items"]:
        if item.get("is_email"):
            continue
        value = answers.get(item["entry"], "")
        if isinstance(value, list):
            value = "; ".join(value)
        row[item["title"]] = value
    return row

@app.post("/api/quiz/submit")
async def quiz_submit(req: QuizSubmitRequest, request: Request):
    """Native quiz: validate -> forward to Google -> store against the OAuth email."""
    quiz = session_state.get("quiz")
    if not session_state["is_active"] or not quiz:
        raise HTTPException(status_code=400, detail="Quiz is not active")
    if not (session_state["db_file"] or session_state["db_table"]):
        raise HTTPException(status_code=400, detail="Session database is not initialized")
    try:
        data = serializer.loads(req.token, max_age=1800)
        email = data["email"]
        device_id = data["device_id"]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    deadline = data.get("deadline")
    if deadline is not None and time.time() > deadline:
        raise HTTPException(status_code=400, detail="Time is up")
    answers = normalize_quiz_answers(quiz, req.answers)

    try:
        conn = db_connect()
        try:
            cursor = conn.cursor()
            cursor.execute(sql_params(f"SELECT 1 FROM {session_table()} WHERE email = ?"), (email,))
            if cursor.fetchone():
                raise HTTPException(status_code=409, detail="Attendance is already marked")
        finally:
            conn.close()
    except HTTPException:
        raise
    except DB_ERROR:
        raise HTTPException(status_code=400, detail="Session database is not initialized")

    payload = build_form_response_data(quiz, answers, email)
    ok, detail = await asyncio.to_thread(submit_to_google, session_state["form_view_url"], payload)
    if not ok:
        raise HTTPException(status_code=502, detail=f"Could not submit to Google Forms: {detail}")

    stored = quiz_answers_for_csv(quiz, answers, email)
    try:
        conn = db_connect()
        try:
            conn.execute(
                sql_params(f"INSERT INTO {session_table()} (email, device_id, form_submitted, ip_address, quiz_answers) VALUES (?, ?, ?, ?, ?)"),
                (email, device_id, "True (Quiz)", get_client_ip(request), json.dumps(stored, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()
    except DB_INTEGRITY_ERROR:
        raise HTTPException(status_code=409, detail="Attendance is already marked")
    except DB_ERROR:
        raise HTTPException(status_code=400, detail="Session database is not initialized")
    return {"status": "ok", "email": email}

# Form validation requires python-multipart
@app.post("/finalize-attendance", response_class=HTMLResponse)
async def finalize_attendance(request: Request, token: str, form_submitted: str = Form("False (Unknown)")):
    def render_status(msg, success=False, email=None):
        return templates.TemplateResponse(request=request, name="status.html", context={"message": msg, "success": success, "email": email})

    try:
        data = serializer.loads(token, max_age=1800)
        email = data["email"]
        device_id = data["device_id"]
    except Exception:
        return render_status("Session expired.", success=False)

    # Reject when no session is running (no storage target) or the admin
    # already stopped it — otherwise sqlite3.connect("") silently creates a
    # temp empty database and the INSERT explodes with "no such table: attendance" (500).
    if not session_state["is_active"] or not (session_state["db_file"] or session_state["db_table"]):
        return render_status("The attendance session has been closed.", success=False, email=email)

    try:
        conn = db_connect()
        try:
            conn.execute(sql_params(f"INSERT INTO {session_table()} (email, device_id, form_submitted, ip_address, quiz_answers) VALUES (?, ?, ?, ?, ?)"),
                         (email, device_id, form_submitted, get_client_ip(request), None))
            conn.commit()
        finally:
            conn.close()
    except DB_INTEGRITY_ERROR:
        return render_status("Error: You or this device have already marked attendance for this session.", success=False, email=email)
    except DB_ERROR:
        return render_status("The attendance session has been closed.", success=False, email=email)

    return render_status("Attendance Marked!", success=True, email=email)
    
if __name__ == "__main__":
    import uvicorn
    # Here is the 0.0.0.0 you are looking for!
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
