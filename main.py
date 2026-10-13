from fastapi import FastAPI, Request, HTTPException, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from itsdangerous import URLSafeTimedSerializer
from authlib.integrations.starlette_client import OAuth
import time
import sqlite3
import os

app = FastAPI()

# --- Configuration ---
SECRET_KEY = "your-super-secret-key"
ALLOWED_EMAIL_DOMAIN = "@goa.bits-pilani.ac.in"
QR_LIFESPAN_SECONDS = 45

# Authlib requires SessionMiddleware to store temporary codes and states
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

# --- Google OAuth Setup ---
oauth = OAuth()
oauth.register(
    name='google',
    client_id=os.getenv("YOUR_GOOGLE_CLIENT_ID"), 
    client_secret=os.getenv("YOUR_GOOGLE_CLIENT_SECRET"),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

serializer = URLSafeTimedSerializer(SECRET_KEY)
consumed_tokens = set()

# --- Setup Templates ---
if not os.path.exists("templates"):
    os.makedirs("templates")
templates = Jinja2Templates(directory="templates")

# --- Database Setup ---
def init_db():
    conn = sqlite3.connect('attendance.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS attendance
                 (id INTEGER PRIMARY KEY, email TEXT UNIQUE, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()
init_db()

# --- Endpoints ---

# 1. ADMIN: Serve the UI Dashboard
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    try:
        return templates.TemplateResponse(
            request=request, 
            name="admin_dashboard.html", 
            context={"request": request}
        )
    except Exception as e:
        return HTMLResponse(f"<h1>Template Error</h1><p>Details: {e}</p>")

# 2. ADMIN: Generate Rotating Token
@app.get("/api/admin/generate-qr-token")
async def generate_qr_token(request: Request):
    nonce = str(time.time())
    token = serializer.dumps({"nonce": nonce})
    
    base_url = str(request.base_url).rstrip("/")
    scan_url = f"{base_url}/scan?token={token}"
    
    return {"token": token, "scan_url": scan_url, "expires_in": QR_LIFESPAN_SECONDS}

# 3. STUDENT: Scan QR and lock device
@app.get("/scan")
async def scan_qr(request: Request, token: str):
    # Validate Token
    try:
        data = serializer.loads(token, max_age=QR_LIFESPAN_SECONDS)
    except Exception:
        raise HTTPException(status_code=403, detail="QR Code Expired. Please wait for the screen to refresh and scan again.")

    # Prevent double scanning
    nonce = data["nonce"]
    if nonce in consumed_tokens:
        raise HTTPException(status_code=403, detail="This QR code was already scanned. Please scan the newest one on the screen.")
    consumed_tokens.add(nonce)

    # Issue device lock cookie
    client_ip = request.headers.get("X-Forwarded-For", request.client.host)
    device_session = serializer.dumps({"scanned_at": time.time(), "ip": client_ip})
    
    response = RedirectResponse(url="/login")
    response.set_cookie(key="device_session", value=device_session, httponly=True, max_age=300)
    
    return response

# 4. STUDENT: Trigger Google OAuth
@app.get("/login")
async def login(request: Request, device_session: str = Cookie(None)):
    if not device_session:
        raise HTTPException(status_code=403, detail="Device not verified. You must scan the QR code first.")
    
    redirect_uri = request.url_for('auth_callback')
    return await oauth.google.authorize_redirect(request, str(redirect_uri))

# 5. STUDENT: Google Auth Callback & Record Attendance
@app.get("/auth/callback")
async def auth_callback(request: Request, device_session: str = Cookie(None)):
    # Verify Device Session
    if not device_session:
        raise HTTPException(status_code=403, detail="Invalid session. You must scan the QR code on the projector.")
    
    # Process Google Auth
    token = await oauth.google.authorize_access_token(request)
    user = token.get('userinfo')
    
    if not user or not user.get('email'):
        raise HTTPException(status_code=400, detail="Failed to fetch user email from Google.")
        
    email = user.get('email')
    
    # Verify Domain
    if not email.endswith(ALLOWED_EMAIL_DOMAIN):
        raise HTTPException(status_code=403, detail=f"Please use your {ALLOWED_EMAIL_DOMAIN} account.")
    
    # Record in DB
    try:
        conn = sqlite3.connect('attendance.db')
        c = conn.cursor()
        c.execute("INSERT INTO attendance (email) VALUES (?)", (email,))
        conn.commit()
    except sqlite3.IntegrityError:
        return {"message": f"Attendance already marked for {email} today!"}
    finally:
        conn.close()
        
    return {"message": f"Attendance marked successfully for {email}!"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
