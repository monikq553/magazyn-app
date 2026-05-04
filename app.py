import os
import logging
from flask import Flask, render_template, request, redirect, session, jsonify
from functools import wraps
import psycopg2
from werkzeug.security import generate_password_hash
from datetime import datetime
import json
from urllib import request as urlrequest
from urllib import error as urlerror
import firebase_admin
from firebase_admin import credentials, auth

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "fallback-secret")

INVESTMENT_WAREHOUSE = "Inwestycja Suwaj"

# 🔥 Firebase config (frontend)
FIREBASE_CONFIG = {
    "apiKey": os.environ.get("FIREBASE_API_KEY"),
    "authDomain": os.environ.get("FIREBASE_AUTH_DOMAIN"),
    "projectId": os.environ.get("FIREBASE_PROJECT_ID"),
    "storageBucket": os.environ.get("FIREBASE_STORAGE_BUCKET"),
    "messagingSenderId": os.environ.get("FIREBASE_MESSAGING_SENDER_ID"),
    "appId": os.environ.get("FIREBASE_APP_ID"),
    "measurementId": os.environ.get("FIREBASE_MEASUREMENT_ID"),
}

ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
ALLOWED_EMAILS = {e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()}

FIREBASE_ADMIN_READY = False
FIREBASE_ADMIN_ERROR = ""


# 🔥 POPRAWIONA FUNKCJA (KLUCZOWA)
def init_firebase_admin():
    global FIREBASE_ADMIN_READY, FIREBASE_ADMIN_ERROR

    if firebase_admin._apps:
        FIREBASE_ADMIN_READY = True
        return

    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")

    if not raw:
        FIREBASE_ADMIN_READY = False
        FIREBASE_ADMIN_ERROR = "Brak FIREBASE_SERVICE_ACCOUNT_JSON"
        print("❌ FIREBASE_SERVICE_ACCOUNT_JSON NIE ISTNIEJE")
        return

    try:
        service_account = json.loads(raw)

        # naprawa \n w kluczu
        if "private_key" in service_account:
            service_account["private_key"] = service_account["private_key"].replace("\\n", "\n")

        cred = credentials.Certificate(service_account)
        firebase_admin.initialize_app(cred)

        FIREBASE_ADMIN_READY = True
        FIREBASE_ADMIN_ERROR = ""
        print("✅ Firebase Admin działa")

    except Exception as e:
        FIREBASE_ADMIN_READY = False
        FIREBASE_ADMIN_ERROR = str(e)
        print("❌ Firebase error:", e)


def verify_id_token_with_firebase_rest(id_token):
    api_key = FIREBASE_CONFIG.get("apiKey")
    endpoint = f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={api_key}"

    payload = json.dumps({"idToken": id_token}).encode("utf-8")

    req = urlrequest.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    with urlrequest.urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    user = data["users"][0]

    return {
        "email": user.get("email"),
        "uid": user.get("localId")
    }


init_firebase_admin()


# 🔥 DB
def db():
    return psycopg2.connect(os.environ.get("DATABASE_URL"))


# 🔒 LOGIN REQUIRED
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


# 🔐 LOGIN
@app.route('/login')
def login():
    return render_template(
        "login.html",
        firebase_config=FIREBASE_CONFIG,
        firebase_admin_ready=FIREBASE_ADMIN_READY,
        firebase_admin_error=FIREBASE_ADMIN_ERROR
    )


@app.route('/auth/session', methods=['POST'])
def create_session():
    data = request.get_json()
    id_token = data.get("idToken")

    try:
        if FIREBASE_ADMIN_READY:
            decoded = auth.verify_id_token(id_token)
            email = decoded["email"]
            uid = decoded["uid"]
        else:
            decoded = verify_id_token_with_firebase_rest(id_token)
            email = decoded["email"]
            uid = decoded["uid"]

        session['user'] = email
        session['uid'] = uid
        session['role'] = "admin" if email in ADMIN_EMAILS else "employee"

        return jsonify({"ok": True})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 401


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


@app.before_request
def require_login():
    allowed = ['login', 'create_session', 'static']
    if request.endpoint not in allowed and 'user' not in session:
        return redirect('/login')


@app.route('/')
@login_required
def home():
    return render_template("home.html")


if __name__ == '__main__':
    app.run()
