# MindAI — Mental Health AI Companion
## Production-Ready Setup Guide

---

## Project Structure

```
mindsync/
├── backend/
│   ├── app.py              ← FastAPI backend (all APIs)
│   ├── sentimnet.py        ← BERT sentiment analysis
│   ├── requirements.txt    ← Python dependencies
│   ├── .env.example        ← Copy to .env and fill values
│   ├── emotion_model.pkl   ← (you provide) Voice emotion model
│   └── scaler.pkl          ← (you provide) Voice model scaler
└── frontend/
    └── index.html          ← Complete frontend (single file)
```

---

## Backend Setup

### 1. Install dependencies
```bash
cd backend
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env and set your NVIDIA API key and JWT secret
```

### 3. Run the server
```bash
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

The database (`mindsync.db`) is created automatically on first run.

---

## Frontend Setup

Just open `frontend/index.html` in any browser.

If the backend runs on a different host/port, edit this line at the top of the `<script>`:
```js
const BASE_URL = 'http://localhost:8000';
```

---

## API Endpoints

### Public (no auth)
| Method | Route        | Description              |
|--------|--------------|--------------------------|
| GET    | /            | Health check             |
| GET    | /health      | Health + model status    |
| POST   | /auth/register | Register new user      |
| POST   | /auth/login  | Login, returns JWT token |
| POST   | /sentiment   | Text sentiment analysis  |
| POST   | /face        | Face emotion detection   |
| POST   | /voice       | Voice emotion detection  |

### Protected (Bearer token required)
| Method | Route                          | Description                    |
|--------|--------------------------------|--------------------------------|
| GET    | /auth/me                       | Current user info              |
| POST   | /chat                          | Chat with AI                   |
| POST   | /generate-report               | Generate session report        |
| GET    | /reports                       | List user's reports            |
| DELETE | /reports/{id}                  | Delete a report                |
| GET    | /sessions                      | List user's sessions           |
| GET    | /sessions/{session_id}/messages | Load session messages         |
| POST   | /reset                         | Clear session chat memory      |
| POST   | /find-doctors                  | Find doctors + helplines       |

---

## Key Fixes Made (vs original code)

### Authentication
- **Before:** localStorage-only login with btoa() password (insecure, not server-verified)
- **After:** JWT tokens, bcrypt hashed passwords, server-side validation, token expiry

### Report Isolation (Critical Bug)
- **Before:** CSV-based storage, no user separation, reports mixed between users
- **After:** SQLite with `user_id` foreign key on every table. All queries filter by authenticated user. Users can NEVER see each other's data.

### Report Generation
- **Before:** Read from CSV, often failed silently
- **After:** Reads from DB using JWT user_id + session_id. Returns structured JSON. Falls back gracefully if LLM returns non-JSON.

### 404 Errors
- **Before:** Routes inconsistent between frontend and backend
- **After:** Centralized `API` config object in frontend, all routes verified against backend.

### Doctor Search Fallback
- **Before:** Failed silently if LLM didn't return JSON
- **After:** Comprehensive fallback with Practo, MindPeers, YourDOST and all 4 helplines always shown.

### Temp File Leak
- **Before:** /voice created temp files that were never deleted
- **After:** try/finally ensures cleanup even on errors.

### Global Chat History
- **Before:** Single global list shared across ALL users
- **After:** Keyed by `user_id:session_id` — fully isolated per user.

### Frontend Stability
- **Before:** No loading states, no error handling, double-send possible
- **After:** `isSending` flag prevents double sends, all errors shown in UI, retry-friendly.

---

## Security Notes

- Change `JWT_SECRET` in `.env` before deploying to production
- Use HTTPS in production (add an nginx/caddy reverse proxy)
- The `allow_origins=["*"]` CORS setting is fine for development; restrict it in production
- Passwords are hashed with bcrypt (cost factor 12)

---

## Voice Model Note

`emotion_model.pkl` and `scaler.pkl` are your existing trained sklearn models.
Place them in the `backend/` directory. The server starts fine without them
(with a warning) and the `/voice` endpoint returns a 503 instead of crashing.
