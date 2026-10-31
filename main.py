from fastapi import FastAPI, Request, HTTPException, Cookie, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from itsdangerous import URLSafeTimedSerializer
from authlib.integrations.starlette_client import OAuth
import sqlite3
import time
import os
import secrets

# ALLOW LOCAL OAUTH OVER HTTP FOR DEBUGGING
os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"

app = FastAPI()

# --- Configuration ---
SECRET_KEY = "your-super-secret-key"
ALLOWED_EMAIL_DOMAIN = "@goa.bits-pilani.ac.in"

# Fallback to defaults if env vars are missing locally
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
GOOGLE_FORM_URL = os.getenv("GOOGLE_FORM_URL", "google_form_link_here")

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
serializer = URLSafeTimedSerializer(SECRET_KEY)
consumed_tokens = set()

# --- Google OAuth Setup ---
oauth = OAuth()
oauth.register(
    name='google',
    # PASTE YOUR LOCAL TESTING KEYS IN THE SECOND ARGUMENT HERE:
    client_id=os.getenv("YOUR_GOOGLE_CLIENT_ID", "google_client_id_here"),
    client_secret=os.getenv("YOUR_GOOGLE_CLIENT_SECRET", "google_client_secret_here"),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile', 'prompt': 'select_account'}
)

if not os.path.exists("templates"):
    os.makedirs("templates")
templates = Jinja2Templates(directory="templates")

# --- Local SQLite Setup ---
def init_db():
    conn = sqlite3.connect('attendance.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS attendance
                 (id INTEGER PRIMARY KEY, email TEXT UNIQUE, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()
init_db()

# --- Admin Authentication ---
def verify_admin_header(request: Request):
    auth_header = request.headers.get("X-Admin-Password")
    if not auth_header or not secrets.compare_digest(auth_header, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

# --- Endpoints ---

# 1. ADMIN: Serve the UI Dashboard
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    return templates.TemplateResponse(
        request=request, 
        name="admin_dashboard.html"
    )

# 2. ADMIN: Generate Rotating Token (Dynamic Lifespan)
@app.get("/api/admin/generate-qr-token")
async def generate_qr_token(request: Request, lifespan: int = 45, authorized: bool = Depends(verify_admin_header)):
    nonce = str(time.time())
    token = serializer.dumps({"nonce": nonce, "lifespan": lifespan})
    
    base_url = str(request.base_url).rstrip("/")
    scan_url = f"{base_url}/scan?token={token}"
    return {"token": token, "scan_url": scan_url, "expires_in": lifespan}

# 3. STUDENT: Scan QR and lock device
@app.get("/scan")
async def scan_qr(request: Request, token: str):
    try:
        data = serializer.loads(token, max_age=86400)
        nonce = float(data["nonce"])
        lifespan = int(data.get("lifespan", 45))
        
        if time.time() - nonce > lifespan:
            raise Exception("Expired")
    except Exception:
        raise HTTPException(status_code=403, detail="QR Code Expired. Please scan the newest one on the screen.")

    if data["nonce"] in consumed_tokens:
        raise HTTPException(status_code=403, detail="QR already scanned.")
    consumed_tokens.add(data["nonce"])

    client_ip = request.headers.get("X-Forwarded-For", request.client.host)
    device_session = serializer.dumps({"scanned_at": time.time(), "ip": client_ip})
    
    response = RedirectResponse(url="/login")
    response.set_cookie(key="device_session", value=device_session, httponly=True, max_age=300)
    return response

# 4. STUDENT: Login Trigger
@app.get("/login")
async def login(request: Request, device_session: str = Cookie(None)):
    if not device_session:
        raise HTTPException(status_code=403, detail="Device not verified. Scan the QR code first.")
    redirect_uri = request.url_for('auth_callback')
    return await oauth.google.authorize_redirect(request, str(redirect_uri))

# 5. STUDENT: Google Auth Callback -> Show Form
@app.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback(request: Request, device_session: str = Cookie(None)):
    def render_status(msg, success=False, email=None):
        return templates.TemplateResponse(
            request=request, 
            name="status.html", 
            context={"message": msg, "success": success, "email": email}
        )

    if not device_session:
        return render_status("Invalid session.", success=False)
    
    try:
        token = await oauth.google.authorize_access_token(request)
        user = token.get('userinfo')
    except Exception:
        return render_status("Google authentication failed.", success=False)
    
    email = user.get('email')
    
    if not email.endswith(ALLOWED_EMAIL_DOMAIN):
        return render_status(f"Please use your {ALLOWED_EMAIL_DOMAIN} account.", success=False)
    
    form_token = serializer.dumps({"email": email})
    return templates.TemplateResponse(
        request=request,
        name="form.html", 
        context={"email": email, "form_token": form_token, "form_url": GOOGLE_FORM_URL}
    )

# 6. STUDENT: Finalize Attendance (Changed to POST)
@app.post("/finalize-attendance", response_class=HTMLResponse)
async def finalize_attendance(request: Request, token: str):
    def render_status(msg, success=False, email=None):
        return templates.TemplateResponse(
            request=request, 
            name="status.html", 
            context={"message": msg, "success": success, "email": email}
        )

    try:
        data = serializer.loads(token, max_age=1800)
        email = data["email"]
    except Exception:
        return render_status("Session expired. You took too long.", success=False)
        
    try:
        conn = sqlite3.connect('attendance.db')
        c = conn.cursor()
        c.execute("INSERT INTO attendance (email) VALUES (?)", (email,))
        conn.commit()
    except sqlite3.IntegrityError:
        return render_status("Attendance already marked today.", success=True, email=email)
    finally:
        conn.close()
        
    return render_status("Form Submitted & Attendance Marked!", success=True, email=email)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
