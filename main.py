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
import csv

os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"

app = FastAPI()
SECRET_KEY = "your-super-secret-key"
ALLOWED_EMAIL_DOMAIN = "@goa.bits-pilani.ac.in"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
serializer = URLSafeTimedSerializer(SECRET_KEY)
consumed_tokens = set()

# Tracks the dynamic DB file for the current session
session_state = {
    "is_active": False,
    "form_url": "",
    "timeout": 300,
    "allowed_ip": "",
    "db_file": ""
}

oauth = OAuth()
oauth.register(
    name='google',
    client_id=os.getenv("YOUR_GOOGLE_CLIENT_ID", "google_client_id_here"),
    client_secret=os.getenv("YOUR_GOOGLE_CLIENT_SECRET", "google_client_secret_here"),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile', 'prompt': 'select_account'}
)

if not os.path.exists("templates"):
    os.makedirs("templates")
templates = Jinja2Templates(directory="templates")

def render_status(request, msg, success=False, email=None):
    return templates.TemplateResponse(request=request, name="status.html", context={"message": msg, "success": success, "email": email})

def verify_admin_header(request: Request):
    auth_header = request.headers.get("X-Admin-Password")
    if not auth_header or not secrets.compare_digest(auth_header, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

def get_client_ip(request: Request):
    # Try multiple headers in order of reliability behind proxies
    for header in ["X-Forwarded-For", "X-Real-IP", "CF-Connecting-IP", "True-Client-IP"]:
        val = request.headers.get(header)
        if val:
            ip = val.split(",")[0].strip()
            if ip and ip != "unknown":
                return ip
    return request.client.host if request.client else "unknown"

class StartSessionRequest(BaseModel):
    form_url: str
    timeout: int
    allowed_ip: str

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="admin_dashboard.html")

@app.post("/api/admin/start")
async def start_session(req: StartSessionRequest, authorized: bool = Depends(verify_admin_header)):
    # Create a brand new DB file for this specific session
    db_filename = f"Attendance_{datetime.now().strftime('%Y_%m_%d_%H_%M')}.db"
    
    session_state["is_active"] = True
    session_state["form_url"] = req.form_url.strip()
    session_state["timeout"] = req.timeout
    session_state["allowed_ip"] = req.allowed_ip.strip()
    session_state["db_file"] = db_filename
    
    # Initialize the schema in the new file
    conn = sqlite3.connect(db_filename)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS attendance
                 (id INTEGER PRIMARY KEY, 
                  email TEXT UNIQUE, 
                  device_id TEXT UNIQUE, 
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                  form_submitted TEXT,
                  ip_address TEXT)''')
    conn.commit()
    conn.close()
    
    return {"status": "started", "db_file": db_filename}

@app.post("/api/admin/stop")
async def stop_session(authorized: bool = Depends(verify_admin_header)):
    session_state["is_active"] = False
    
    # Extract data from the session's specific DB file
    conn = sqlite3.connect(session_state["db_file"])
    c = conn.cursor()
    c.execute("SELECT id, email, device_id, timestamp, form_submitted, ip_address FROM attendance")
    rows = c.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Email", "Device Fingerprint", "Timestamp (UTC)", "Form Submitted", "IP Address"])
    for row in rows:
        writer.writerow(row)
    output.seek(0)
    
    filename = session_state["db_file"].replace(".db", ".csv")
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.get("/api/admin/generate-qr-token")
async def generate_qr_token(request: Request, lifespan: int = 45, authorized: bool = Depends(verify_admin_header)):
    if not session_state["is_active"]:
        raise HTTPException(status_code=400, detail="Session not active")
        
    nonce = str(time.time())
    token = serializer.dumps({"nonce": nonce, "lifespan": lifespan})
    return {"token": token, "scan_url": f"{str(request.base_url).rstrip('/')}/scan?token={token}", "expires_in": lifespan}

@app.get("/api/check-ip")
async def check_ip(request: Request):
    client_ip = get_client_ip(request)
    return {
        "detected_ip": client_ip,
        "x_forwarded_for": request.headers.get("X-Forwarded-For", ""),
        "x_real_ip": request.headers.get("X-Real-IP", ""),
        "raw_client_host": request.client.host if request.client else "",
        "allowed_prefix": session_state.get("allowed_ip", ""),
        "wifi_match": client_ip.startswith(session_state.get("allowed_ip", "")) if session_state.get("allowed_ip") else None
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

    if data["nonce"] in consumed_tokens:
        return render_status(request, "This QR code has already been scanned. Please scan the newest one.")
    consumed_tokens.add(data["nonce"])

    # Lock the Device Fingerprint
    device_id = request.cookies.get("device_id")
    if not device_id:
        device_id = secrets.token_hex(16)

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
    allowed prefix. Falls back to the public IP the server observed."""
    if not session_state["is_active"]:
        return JSONResponse({"ok": False, "message": "The attendance session has been closed."})

    try:
        data = serializer.loads(net_token, max_age=300)
        device_id = data["device_id"]
        public_ip = data["ip"]
    except Exception:
        return JSONResponse({"ok": False, "message": "Network check expired. Please scan the QR code again."})

    allowed_ip = (session_state.get("allowed_ip") or "").strip()
    if allowed_ip:
        local_ip = (local_ip or "").strip()
        local_ok = bool(local_ip) and local_ip.startswith(allowed_ip)
        public_ok = public_ip.startswith(allowed_ip)
        if not (local_ok or public_ok):
            return JSONResponse({
                "ok": False,
                "message": "You must be connected to the required WiFi network to mark attendance.",
                "expected": allowed_ip,
                "local_ip": local_ip or "not detected",
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
    
    form_token = serializer.dumps({"email": email, "device_id": device_id})
    
    # If NO form is required, insert directly
    if not session_state["form_url"]:
        try:
            conn = sqlite3.connect(session_state["db_file"])
            c = conn.cursor()
            c.execute("INSERT INTO attendance (email, device_id, form_submitted, ip_address) VALUES (?, ?, ?, ?)", 
                      (email, device_id, "N/A (No Form)", get_client_ip(request)))
            conn.commit()
            conn.close()
        except sqlite3.IntegrityError:
            return render_status("Error: You or this device have already marked attendance today.", success=False, email=email)
        return render_status("Attendance Marked Successfully!", success=True, email=email)

    return templates.TemplateResponse(request=request, name="form.html", context={
        "email": email, 
        "form_token": form_token, 
        "form_url": session_state["form_url"],
        "timeout_seconds": session_state["timeout"]
    })

# Track which form tokens have been confirmed as submitted
form_confirmed = set()

@app.post("/api/form/confirm")
async def confirm_form(token: str = Form(...)):
    try:
        data = serializer.loads(token, max_age=1800)
        form_confirmed.add(data["email"])
        return {"status": "confirmed"}
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or expired token")

@app.post("/api/form/status")
async def form_status(token: str = Form(...)):
    try:
        data = serializer.loads(token, max_age=1800)
        confirmed = data["email"] in form_confirmed
        return {"confirmed": confirmed}
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or expired token")

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
        
    try:
        conn = sqlite3.connect(session_state["db_file"])
        c = conn.cursor()
        c.execute("INSERT INTO attendance (email, device_id, form_submitted, ip_address) VALUES (?, ?, ?, ?)", 
                  (email, device_id, form_submitted, get_client_ip(request)))
        conn.commit()
    except sqlite3.IntegrityError:
        return render_status("Error: You or this device have already marked attendance for this session.", success=False, email=email)
    finally:
        conn.close()
        
    return render_status("Attendance Marked!", success=True, email=email)
    
if __name__ == "__main__":
    import uvicorn
    # Here is the 0.0.0.0 you are looking for!
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
