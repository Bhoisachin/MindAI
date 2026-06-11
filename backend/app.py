import os, json, re, asyncio, tempfile, logging
from datetime import datetime, timedelta, timezone
import numpy as np
import cv2
import librosa
import joblib
import httpx
import aiosqlite
import bcrypt
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, field_validator
from dotenv import load_dotenv
import jwt
from deepface import DeepFace

try:
    from sentimnet import get_sentiment as _get_sentiment
except ImportError:
    def _get_sentiment(text: str) -> str:
        return "Neutral"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("mindsync")

load_dotenv()

NVIDIA_API_KEY  = os.getenv("API_KEY", "")
LLM_URL         = "https://integrate.api.nvidia.com/v1/chat/completions"
LLM_MODEL       = os.getenv("LLM_MODEL", "meta/llama-3.3-70b-instruct")
JWT_SECRET      = os.getenv("JWT_SECRET", "mindsync-super-secret-change-in-prod")
JWT_ALGORITHM   = "HS256"
JWT_EXPIRE_DAYS = 7
DB_PATH         = os.getenv("DB_PATH", "mindsync.db")
MAX_HISTORY     = 6

try:
    voice_model  = joblib.load("emotion_model.pkl")
    voice_scaler = joblib.load("scaler.pkl")
    VOICE_LOADED = True
    logger.info("Voice model loaded.")
except FileNotFoundError:
    voice_model = voice_scaler = None
    VOICE_LOADED = False
    logger.warning("Voice model not found — /voice returns fallback.")

_chat_sessions: dict[str, list] = {}

# Shared async HTTP client — reuses connections, avoids per-request overhead
_http_client: httpx.AsyncClient | None = None

def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
    return _http_client

# ─────────────────────────────────────────────
# PREPROCESSING HELPERS
# ─────────────────────────────────────────────

def preprocess_text(text: str) -> str:
    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(r'http\S+', '', text)
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r"[^\w\s,.!?'\-]", '', text)
    contractions = {
        "won't": "will not", "can't": "cannot", "i'm": "i am",
        "it's": "it is", "don't": "do not", "doesn't": "does not",
        "didn't": "did not", "i've": "i have", "i'll": "i will",
    }
    t = text.lower()
    for k, v in contractions.items():
        t = t.replace(k, v)
    return t[:512].strip()


def preprocess_image(raw_bytes: bytes) -> np.ndarray | None:
    arr = np.frombuffer(raw_bytes, np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        return None
    try:
        h, w = frame.shape[:2]
        if max(h, w) > 640:
            scale = 640 / max(h, w)
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        blur_score = cv2.Laplacian(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
        if blur_score >= 80:
            frame = cv2.fastNlMeansDenoisingColored(frame, None, 7, 7, 7, 21)
        lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        l = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l)
        frame = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)
    except Exception as e:
        logger.warning("Image preprocessing partial failure: %s", e)
    return frame


def preprocess_audio(raw_bytes: bytes, target_sr: int = 22050) -> tuple:
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
            f.write(raw_bytes)
            tmp = f.name
        audio, sr = librosa.load(tmp, sr=target_sr, mono=True)
        audio, _ = librosa.effects.trim(audio, top_db=25)
        peak = np.max(np.abs(audio))
        if peak > 0:
            audio = audio / peak * 0.9
        return audio, sr
    except Exception as e:
        logger.warning("Audio preprocessing failed: %s — loading raw.", e)
        try:
            audio, sr = librosa.load(tmp, sr=target_sr)
            return audio, sr
        except Exception:
            return None, None
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


def get_sentiment(text: str) -> str:
    cleaned = preprocess_text(text)
    if not cleaned:
        return "Neutral"
    return _get_sentiment(cleaned)


# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                email      TEXT    NOT NULL UNIQUE,
                username   TEXT    NOT NULL UNIQUE,
                password   TEXT    NOT NULL,
                created_at TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL REFERENCES users(id),
                session_id TEXT    NOT NULL,
                started_at TEXT    NOT NULL,
                ended_at   TEXT,
                UNIQUE(user_id, session_id)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL REFERENCES users(id),
                session_id TEXT    NOT NULL,
                role       TEXT    NOT NULL,
                content    TEXT    NOT NULL,
                sentiment  TEXT,
                face       TEXT,
                voice      TEXT,
                created_at TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reports (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL REFERENCES users(id),
                session_id      TEXT    NOT NULL,
                severity_score  INTEGER,
                severity_level  TEXT,
                report_json     TEXT    NOT NULL,
                created_at      TEXT    NOT NULL
            );
        """)
        await db.commit()
    logger.info("Database ready at %s", DB_PATH)


async def get_session_history(user_id: int, session_id: str) -> list:
    key = f"{user_id}:{session_id}"
    if key not in _chat_sessions:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT role, content FROM messages WHERE user_id=? AND session_id=? ORDER BY id",
                (user_id, session_id)
            ) as cur:
                rows = await cur.fetchall()
        _chat_sessions[key] = [
            {"role": r[0], "content": r[1]}
            for r in rows if r[0] in ("user", "assistant")
        ]
    return _chat_sessions[key]


def clear_session(user_id: int, session_id: str):
    _chat_sessions.pop(f"{user_id}:{session_id}", None)


# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────

security = HTTPBearer(auto_error=False)

def create_token(user_id: int, username: str) -> str:
    return jwt.encode(
        {"sub": str(user_id), "username": username,
         "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS)},
        JWT_SECRET, algorithm=JWT_ALGORITHM
    )

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired. Please sign in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token.")

async def get_current_user(creds: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    if not creds:
        raise HTTPException(401, "Authorization header missing.")
    payload = decode_token(creds.credentials)
    uid = int(payload["sub"])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, name, email, username FROM users WHERE id=?", (uid,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(401, "User not found.")
    return {"id": row[0], "name": row[1], "email": row[2], "username": row[3]}


# ─────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────

class RegisterInput(BaseModel):
    name: str; email: str; username: str; password: str

    @field_validator("name", "email", "username", "password")
    @classmethod
    def not_empty(cls, v):
        if not v.strip(): raise ValueError("Field must not be empty")
        return v.strip()

    @field_validator("password")
    @classmethod
    def min_length(cls, v):
        if len(v) < 6: raise ValueError("Password must be at least 6 characters")
        return v

class LoginInput(BaseModel):
    username: str; password: str

class TextInput(BaseModel):
    text: str
    @field_validator("text")
    @classmethod
    def not_empty(cls, v):
        if not v.strip(): raise ValueError("text must not be empty")
        return v.strip()

class ChatInput(BaseModel):
    text: str
    sentiment:  str = "Neutral"
    face:       str = "unknown"
    voice:      str = "not_used"
    session_id: str = "default"

    @field_validator("text")
    @classmethod
    def not_empty(cls, v):
        if not v.strip(): raise ValueError("text must not be empty")
        return v.strip()

class SessionInput(BaseModel):
    session_id: str

class DoctorSearchInput(BaseModel):
    session_id: str; city: str; language: str = "Hindi"; concern: str = ""

    @field_validator("city")
    @classmethod
    def city_not_empty(cls, v):
        if not v.strip(): raise ValueError("city must not be empty")
        return v.strip()


# ─────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────

app = FastAPI(title="MindSync API", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    await init_db()
    get_http_client()  # warm up shared client

@app.on_event("shutdown")
async def shutdown():
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()


# ─────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "ok", "app": "MindSync API v2.1", "voice_model": VOICE_LOADED}

@app.get("/health")
async def health():
    return {"status": "ok", "voice_model": VOICE_LOADED, "db": DB_PATH}


# ─────────────────────────────────────────────
# AUTH ROUTES
# ─────────────────────────────────────────────

@app.post("/auth/register", status_code=201)
async def register(data: RegisterInput):
    hashed = bcrypt.hashpw(data.password.encode(), bcrypt.gensalt()).decode()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO users (name,email,username,password,created_at) VALUES (?,?,?,?,?)",
                (data.name, data.email.lower(), data.username.lower(), hashed, datetime.now().isoformat())
            )
            await db.commit()
            async with db.execute("SELECT id FROM users WHERE username=?", (data.username.lower(),)) as cur:
                row = await cur.fetchone()
        token = create_token(row[0], data.username.lower())
        logger.info("Registered: %s (id=%d)", data.username, row[0])
        return {"token": token, "user": {"id": row[0], "name": data.name, "email": data.email, "username": data.username}}
    except aiosqlite.IntegrityError as e:
        raise HTTPException(400, "Email already registered." if "email" in str(e) else "Username already taken.")


@app.post("/auth/login")
async def login(data: LoginInput):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id,name,email,username,password FROM users WHERE username=?",
            (data.username.lower(),)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(401, "Username not found.")
    if not bcrypt.checkpw(data.password.encode(), row[4].encode()):
        raise HTTPException(401, "Incorrect password.")
    token = create_token(row[0], row[3])
    logger.info("Login: %s", row[3])
    return {"token": token, "user": {"id": row[0], "name": row[1], "email": row[2], "username": row[3]}}


@app.get("/auth/me")
async def me(user=Depends(get_current_user)):
    return user


# ─────────────────────────────────────────────
# AI ANALYSIS ROUTES
# ─────────────────────────────────────────────

@app.post("/sentiment")
async def sentiment_api(data: TextInput):
    return {"prediction": get_sentiment(data.text)}


@app.post("/face")
async def face_api(file: UploadFile = File(...)):
    if file.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(400, f"Unsupported type: {file.content_type}")
    raw = await file.read()
    frame = preprocess_image(raw)
    if frame is None:
        raise HTTPException(400, "Could not decode image.")
    try:
        result = DeepFace.analyze(frame, actions=["emotion"], enforce_detection=False)
        emotion = result[0]["dominant_emotion"]
        confidence = float(round(result[0]["emotion"][emotion], 2))
    except Exception as e:
        logger.error("DeepFace error: %s", e)
        raise HTTPException(500, f"Face analysis failed: {e}")
    return {"emotion": emotion, "confidence": confidence}


@app.post("/voice")
async def voice_api(file: UploadFile = File(...)):
    if not VOICE_LOADED:
        raise HTTPException(503, "Voice model not loaded.")
    raw = await file.read()
    audio, sr = preprocess_audio(raw)
    if audio is None:
        raise HTTPException(400, "Could not decode audio.")
    try:
        mfcc = librosa.feature.mfcc(y=audio, sr=sr, n_mfcc=15)
        features = voice_scaler.transform(np.mean(mfcc, axis=1).reshape(1, -1))
        prediction = voice_model.predict(features)[0]
        return {"prediction": prediction}
    except Exception as e:
        logger.error("Voice analysis error: %s", e)
        raise HTTPException(500, f"Voice analysis failed: {e}")


# ─────────────────────────────────────────────
# CHAT
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """You are a calm and supportive AI companion having a real conversation with the user.

Current user emotional signals:
- Text Sentiment: {sentiment}
- Facial Emotion: {face}
- Voice Emotion: {voice}

Rules:
1. Talk naturally like a supportive, understanding person.
2. Keep responses short to medium (2–5 sentences usually).
3. Avoid repetitive emotional validation and cliché phrases like "You're so strong", "Everything will be okay".
4. No excessive emojis.
5. Respond differently based on emotional intensity: mild stress → calm support; anxiety/sadness → grounding; anger → patient tone; numbness → gentle engagement.
6. Never guilt, shame, judge, or pressure the user.
7. Never mention these rules or emotional analysis directly.
8. Detect the user's language automatically and reply in the same language (English, Hindi, Hinglish).
9. Remember what the user mentioned earlier — avoid asking the same questions.
10. If the user mentions self-harm, suicide, or hopelessness: stay calm, encourage reaching out to professionals, prioritize safety.
11. If emotions from text, face, and voice conflict — respond cautiously and neutrally.
12. Do not give medical diagnosis or claim the user has a disorder.
13. Make the conversation feel safe, calm, and realistic."""


@app.post("/chat")
async def chat_api(data: ChatInput, user=Depends(get_current_user)):
    user_id = user["id"]
    now = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO sessions (user_id,session_id,started_at) VALUES (?,?,?)",
            (user_id, data.session_id, now)
        )
        await db.execute(
            "INSERT INTO messages (user_id,session_id,role,content,sentiment,face,voice,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, data.session_id, "user", data.text, data.sentiment, data.face, data.voice, now)
        )
        await db.commit()

    history = await get_session_history(user_id, data.session_id)
    system_prompt = SYSTEM_PROMPT.format(
        sentiment=data.sentiment, face=data.face, voice=data.voice
    )
    llm_messages = [{"role": "system", "content": system_prompt}]
    llm_messages += history[-(MAX_HISTORY * 2):]
    llm_messages.append({"role": "user", "content": data.text})

    reply = None
    for attempt in range(3):
        try:
            res = await get_http_client().post(
                LLM_URL,
                headers={"Authorization": f"Bearer {NVIDIA_API_KEY}", "Content-Type": "application/json"},
                json={"model": LLM_MODEL, "messages": llm_messages, "temperature": 0.8, "max_tokens": 300},
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
            if res.status_code == 200:
                choices = res.json().get("choices", [])
                if choices:
                    reply = (choices[0]["message"]["content"] or "").strip()
                    if reply:
                        break
        except Exception as e:
            logger.error("LLM attempt %d failed: %s", attempt + 1, e)
        await asyncio.sleep(1)

    if not reply:
        reply = "I'm here with you. It seems I'm having a bit of trouble connecting right now — could you share more about how you're feeling?"

    history.append({"role": "user", "content": data.text})
    history.append({"role": "assistant", "content": reply})

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (user_id,session_id,role,content,created_at) VALUES (?,?,?,?,?)",
            (user_id, data.session_id, "assistant", reply, datetime.now(timezone.utc).isoformat())
        )
        await db.commit()

    return {"reply": reply, "session_id": data.session_id}


# ─────────────────────────────────────────────
# SESSION & HISTORY ROUTES
# ─────────────────────────────────────────────

@app.get("/chat-history/{user_id}")
async def get_chat_history(user_id: int, user=Depends(get_current_user)):
    if user["id"] != user_id:
        raise HTTPException(403, "Access denied.")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id,session_id,role,content,sentiment,face,voice,created_at FROM messages WHERE user_id=? ORDER BY id",
            (user_id,)
        ) as cur:
            rows = await cur.fetchall()
    return [
        {"id": r[0], "session_id": r[1], "role": r[2], "content": r[3],
         "sentiment": r[4], "face": r[5], "voice": r[6], "created_at": r[7]}
        for r in rows
    ]


@app.delete("/chat-history/{user_id}")
async def delete_chat_history(user_id: int, user=Depends(get_current_user)):
    if user["id"] != user_id:
        raise HTTPException(403, "Access denied.")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        await db.commit()
    for k in [k for k in _chat_sessions if k.startswith(f"{user_id}:")]:
        _chat_sessions.pop(k, None)
    return {"deleted": True}


@app.get("/last-session/{user_id}")
async def get_last_session(user_id: int, user=Depends(get_current_user)):
    if user["id"] != user_id:
        raise HTTPException(403, "Access denied.")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT session_id, started_at FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
    return {"session_id": row[0], "started_at": row[1]} if row else {"session_id": None}


@app.get("/sessions")
async def get_sessions(user=Depends(get_current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT s.session_id, s.started_at, COUNT(m.id) as msg_count
               FROM sessions s
               LEFT JOIN messages m ON m.session_id=s.session_id AND m.user_id=s.user_id
               WHERE s.user_id=?
               GROUP BY s.session_id ORDER BY s.id DESC""",
            (user["id"],)
        ) as cur:
            rows = await cur.fetchall()
    return [{"session_id": r[0], "started_at": r[1], "message_count": r[2]} for r in rows]


@app.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, user=Depends(get_current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT role,content,sentiment,face,voice,created_at FROM messages WHERE user_id=? AND session_id=? ORDER BY id",
            (user["id"], session_id)
        ) as cur:
            rows = await cur.fetchall()
    return [{"role": r[0], "content": r[1], "sentiment": r[2], "face": r[3], "voice": r[4], "created_at": r[5]}
            for r in rows]


@app.post("/reset")
async def reset_session(data: SessionInput, user=Depends(get_current_user)):
    clear_session(user["id"], data.session_id)
    return {"message": f"Session '{data.session_id}' cleared."}


# ─────────────────────────────────────────────
# SEVERITY SCORING
# ─────────────────────────────────────────────

_NEG_FACE  = {"sad", "angry", "fear", "disgust"}
_NEG_VOICE = {"sad", "angry", "fearful", "disgust", "negative"}

def _compute_severity(messages: list[dict]) -> dict:
    if not messages:
        return {"score": 0, "level": "Low", "raw_score": 0, "total_entries": 0}
    score = 0
    for m in messages:
        if (m.get("sentiment") or "").lower() == "negative": score += 3
        if (m.get("face") or "").lower() in _NEG_FACE:       score += 4
        if any(n in (m.get("voice") or "").lower() for n in _NEG_VOICE): score += 3
    normalised = min(round(score / (len(messages) * 10) * 100), 100)
    level = "Low" if normalised <= 25 else "Moderate" if normalised <= 50 else "High" if normalised <= 75 else "Critical"
    return {"score": normalised, "level": level, "raw_score": score, "total_entries": len(messages)}


# ─────────────────────────────────────────────
# REPORT ROUTES
# ─────────────────────────────────────────────

@app.post("/generate-report")
async def generate_report(data: SessionInput, user=Depends(get_current_user)):
    user_id = user["id"]
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT role,content,sentiment,face,voice,created_at FROM messages WHERE user_id=? AND session_id=? ORDER BY id",
            (user_id, data.session_id)
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        raise HTTPException(404, "No messages found. Start a conversation first.")

    messages = [{"role": r[0], "content": r[1], "sentiment": r[2], "face": r[3], "voice": r[4], "created_at": r[5]} for r in rows]
    user_msgs = [m for m in messages if m["role"] == "user"]
    severity = _compute_severity(user_msgs)
    convo = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in messages[-20:] if m["role"] in ("user", "assistant")
    ) or "No conversation yet."

    prompt = f"""You are a clinical mental health report generator AI.

SESSION DATA:
- Session ID: {data.session_id}
- User: {user['name']}
- Total messages: {len(user_msgs)}
- Severity Score: {severity['score']}/100
- Severity Level: {severity['level']}
- Session start: {messages[0]['created_at'] if messages else 'N/A'}

EMOTION SIGNALS:
- Sentiments: {', '.join(m.get('sentiment','') for m in user_msgs if m.get('sentiment'))}
- Faces: {', '.join(m.get('face','') for m in user_msgs if m.get('face'))}
- Voices: {', '.join(m.get('voice','') for m in user_msgs if m.get('voice'))}

CONVERSATION (last 20 messages):
{convo}

Return ONLY valid JSON (no markdown) with this structure:
{{
  "report_title": "Mental Health Session Report",
  "generated_at": "{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
  "user_name": "{user['name']}",
  "session_id": "{data.session_id}",
  "severity": {{"score": {severity['score']}, "level": "{severity['level']}", "description": "<2-3 sentence description>"}},
  "emotional_summary": {{"overview": "<3-4 sentences>", "dominant_emotions": ["<e1>","<e2>"], "emotional_consistency": "<consistent|mixed|conflicting>", "key_observations": ["<o1>","<o2>","<o3>"]}},
  "mental_health_indicators": {{"possible_concerns": ["<c1>","<c2>"], "protective_factors": ["<f1>","<f2>"], "risk_notes": "<note or None detected>"}},
  "suggestions": [
    {{"category":"Lifestyle","suggestion":"<action>","reason":"<why>"}},
    {{"category":"Mindfulness","suggestion":"<action>","reason":"<why>"}},
    {{"category":"Social","suggestion":"<action>","reason":"<why>"}},
    {{"category":"Professional","suggestion":"<action>","reason":"<why>"}},
    {{"category":"Daily Habit","suggestion":"<action>","reason":"<why>"}}
  ],
  "doctor_referral": {{"recommended": {"true" if severity['level'] in ('High','Critical') else "false"}, "urgency": "<urgency>", "reason": "<reason>", "specialist_type": "<type>",
    "helplines": [{{"name":"iCall (TISS)","number":"9152987821","available":"Mon-Sat 8am-10pm"}},{{"name":"Vandrevala Foundation","number":"1860-2662-345","available":"24/7"}},{{"name":"AASRA","number":"9820466627","available":"24/7"}}]}},
  "general_wellness_tips": ["<t1>","<t2>","<t3>"],
  "disclaimer": "AI-generated report — not a clinical diagnosis. Consult a licensed professional."
}}"""

    try:
        res = await get_http_client().post(
            LLM_URL,
            headers={"Authorization": f"Bearer {NVIDIA_API_KEY}", "Content-Type": "application/json"},
            json={"model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 2000},
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
        if res.status_code != 200:
            raise HTTPException(502, f"LLM API error {res.status_code}")
        raw = (res.json()["choices"][0]["message"]["content"] or "").strip()
    except httpx.TimeoutException:
        raise HTTPException(504, "Report generation timed out — the LLM took too long. Try again or reduce conversation length.")

    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"): raw = raw[4:]
    raw = raw.strip()

    try:
        report = json.loads(raw)
    except json.JSONDecodeError:
        report = {
            "report_title": "Mental Health Session Report",
            "generated_at": datetime.now().isoformat(),
            "user_name": user["name"],
            "session_id": data.session_id,
            "severity": severity,
            "parse_error": "LLM returned non-JSON.",
            "raw_report": raw
        }

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO reports (user_id,session_id,severity_score,severity_level,report_json,created_at) VALUES (?,?,?,?,?,?)",
            (user_id, data.session_id, severity["score"], severity["level"], json.dumps(report), datetime.now().isoformat())
        )
        await db.commit()

    logger.info("Report generated: user_id=%d session=%s severity=%s", user_id, data.session_id, severity["level"])
    return report


@app.get("/reports")
async def get_reports(user=Depends(get_current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id,session_id,severity_score,severity_level,report_json,created_at FROM reports WHERE user_id=? ORDER BY id DESC",
            (user["id"],)
        ) as cur:
            rows = await cur.fetchall()
    return [{"id": r[0], "session_id": r[1], "severity_score": r[2], "severity_level": r[3],
             "report": json.loads(r[4]), "created_at": r[5]} for r in rows]


@app.delete("/reports/{report_id}")
async def delete_report(report_id: int, user=Depends(get_current_user)):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM reports WHERE id=? AND user_id=?", (report_id, user["id"])) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "Report not found or not yours.")
        await db.execute("DELETE FROM reports WHERE id=?", (report_id,))
        await db.commit()
    return {"deleted": report_id}


# ─────────────────────────────────────────────
# FIND DOCTORS
# ─────────────────────────────────────────────

_SPECIALIST_MAP = {"Low": "Counselor or Life Coach", "Moderate": "Clinical Psychologist", "High": "Psychiatrist", "Critical": "Psychiatrist (Urgent)"}
_FALLBACK_DOCTORS = {
    "online_platforms": [
        {"name": "Practo", "url": "https://practo.com", "specialization": "Psychiatrists & psychologists", "approx_fee": "₹300–₹1500", "available_24_7": False},
        {"name": "MindPeers", "url": "https://mindpeers.co", "specialization": "Online therapy & counseling", "approx_fee": "₹500–₹1200", "available_24_7": False},
        {"name": "YourDOST", "url": "https://yourdost.com", "specialization": "Counseling & emotional support", "approx_fee": "₹400–₹1000", "available_24_7": False},
    ],
    "emergency_helplines": [
        {"name": "iCall (TISS)", "number": "9152987821", "available": "Mon-Sat 8am-10pm", "languages": ["English","Hindi"], "for_situations": "Stress, anxiety, depression"},
        {"name": "Vandrevala Foundation", "number": "1860-2662-345", "available": "24/7", "languages": ["English","Hindi"], "for_situations": "Any mental health crisis"},
        {"name": "AASRA", "number": "9820466627", "available": "24/7", "languages": ["English","Hindi"], "for_situations": "Suicidal crisis, hopelessness"},
        {"name": "Snehi", "number": "044-24640050", "available": "24/7", "languages": ["English","Hindi","Tamil"], "for_situations": "Emotional distress"},
    ]
}


@app.post("/find-doctors")
async def find_doctors(data: DoctorSearchInput, user=Depends(get_current_user)):
    severity_level = "Moderate"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT sentiment,face,voice FROM messages WHERE user_id=? AND session_id=? AND role='user'",
            (user["id"], data.session_id)
        ) as cur:
            rows = await cur.fetchall()
    if rows:
        severity_level = _compute_severity([{"sentiment": r[0], "face": r[1], "voice": r[2]} for r in rows])["level"]

    specialist = _SPECIALIST_MAP.get(severity_level, "Clinical Psychologist")
    urgency_map = {"Low": "general wellness", "Moderate": "stress and anxiety counseling", "High": "urgent mental health support", "Critical": "immediate psychiatric help"}

    prompt = f"""You are a mental health resource finder for India.
User: City={data.city}, Severity={severity_level}, Concern={data.concern or 'mental health'}, Language={data.language}, Specialist={specialist}

Find REAL active mental health professionals in {data.city}, India.
Return ONLY valid JSON (no markdown):
{{
  "city": "{data.city}", "severity_level": "{severity_level}",
  "local_doctors": [{{"name":"<name>","specialization":"<type>","clinic_or_hospital":"<name>","address":"<address>","phone":"<phone or null>","consultation_fee":"<fee>","languages":["{data.language}"],"available_online":false,"source_url":"<url or null>"}}],
  "online_platforms": [{{"name":"Practo","url":"https://practo.com","specialization":"Psychiatrists","approx_fee":"₹300-₹1500","available_24_7":false}},{{"name":"MindPeers","url":"https://mindpeers.co","specialization":"Online therapy","approx_fee":"₹500-₹1200","available_24_7":false}}],
  "emergency_helplines": [{{"name":"iCall","number":"9152987821","available":"Mon-Sat 8am-10pm","languages":["Hindi","English"],"for_situations":"Stress and anxiety"}},{{"name":"Vandrevala Foundation","number":"1860-2662-345","available":"24/7","languages":["Hindi","English"],"for_situations":"Any crisis"}},{{"name":"AASRA","number":"9820466627","available":"24/7","languages":["Hindi","English"],"for_situations":"Suicidal crisis"}}],
  "search_note": "<quality note>"
}}
Only include real verified doctors. Empty local_doctors array if none found."""

    try:
        res = await get_http_client().post(
            LLM_URL,
            headers={"Authorization": f"Bearer {NVIDIA_API_KEY}", "Content-Type": "application/json"},
            json={"model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2, "max_tokens": 1500},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        if res.status_code != 200:
            raise Exception(f"LLM {res.status_code}")
        raw = (res.json()["choices"][0]["message"]["content"] or "").strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"): raw = raw[4:]
        result = json.loads(raw.strip())
    except Exception as e:
        logger.warning("Doctor search failed (%s) — using fallback.", e)
        result = {"city": data.city, "severity_level": severity_level, "local_doctors": [],
                  "search_note": "Live search unavailable. Showing verified fallback.", **_FALLBACK_DOCTORS}

    result["recommended_specialist"] = specialist
    result["session_severity"] = severity_level
    result["search_timestamp"] = datetime.now().isoformat()
    return result