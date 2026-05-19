# ── Imports ───────────────────────────────────────────────────────────────────

import os
import re
import logging
import secrets
import requests
import bcrypt
from flask import Flask, request, jsonify, render_template, session


from flask_cors import CORS                    # Allows browser JS to call this server across ports
from flask_sqlalchemy import SQLAlchemy        # Lets us define DB tables as Python classes
from dotenv import load_dotenv                 # Reads the .env file and loads keys into os.getenv()
from datetime import datetime, timezone, timedelta
#   datetime   → create timestamps
#   timezone   → make all times UTC so they're consistent regardless of server location
#   timedelta  → represent a span of time (used for 30-day session expiry)


# ── Startup ───────────────────────────────────────────────────────────────────

_HERE = os.path.dirname(os.path.abspath(__file__))  # folder this script lives in
load_dotenv()                          # try .env first (standard name)
load_dotenv(os.path.join(_HERE, "_env"))  # also try _env in case the file wasn't renamed

logging.basicConfig(level=logging.INFO)   # Print INFO and above (WARNING, ERROR) to the terminal
logger = logging.getLogger(__name__)      # Create a logger named after this module


# ── App & Config ──────────────────────────────────────────────────────────────

app = Flask(__name__)  # Create the Flask app — __name__ tells Flask where to find templates/static files

app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))
# Secret key signs the session cookie so users can't tamper with it
# Reads from .env — falls back to a random 64-char hex string if not set
# Warning: if a random fallback is used, all sessions break on every server restart

CORS(app, supports_credentials=True)
# Allow cross-origin requests from the browser
# supports_credentials=True is required so the session cookie is sent with API calls

BASE_DIR = _HERE  # reuse the path already computed above
# Get the folder this script lives in — used to build the database path
# abspath makes the path absolute, dirname strips the filename

app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(BASE_DIR, 'cybershield.db')}"
# Tell SQLAlchemy where the database file is — placed in the same folder as this script

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# Disable SQLAlchemy's object change tracker — it's slow and not needed here

db = SQLAlchemy(app)  # Connect SQLAlchemy to Flask — all DB operations go through this object


# ── Database Models ───────────────────────────────────────────────────────────

class User(db.Model):
    __tablename__ = "users"  # The actual SQL table name

    id            = db.Column(db.String(16),  primary_key=True, default=lambda: secrets.token_hex(8))
    # Random 16-char hex ID instead of auto-increment — prevents users from guessing each other's IDs

    email         = db.Column(db.String(255), unique=True, nullable=False)   # Must be unique, can't be empty
    username      = db.Column(db.String(80),  unique=True, nullable=False)   # Must be unique, can't be empty
    password_hash = db.Column(db.String(72),  nullable=False)                # Stores the bcrypt hash, never the plain password
    created_at    = db.Column(db.DateTime,    default=lambda: datetime.now(timezone.utc))  # Auto-set to now (UTC) on creation

    chat_sessions = db.relationship("ChatSession", backref="user", lazy=True, cascade="all, delete-orphan")
    # Links this user to their ChatSession rows
    # cascade="all, delete-orphan" means deleting the user also deletes all their sessions

    def to_dict(self):
        # Converts the User object into a plain dict so it can be returned as JSON
        return {
            "id":         self.id,
            "email":      self.email,
            "username":   self.username,
            "created_at": self.created_at.isoformat() if self.created_at else None  # ISO string or None if missing
        }


class ChatSession(db.Model):
    __tablename__ = "chat_sessions"  # SQL table name

    id         = db.Column(db.Integer,     primary_key=True, autoincrement=True)  # Auto-numbered integer ID
    user_id    = db.Column(db.String(16),  db.ForeignKey("users.id"), nullable=False)  # Links to the User who owns this session
    title      = db.Column(db.String(200), nullable=False, default="New Chat")    # Auto-set from first message
    created_at = db.Column(db.DateTime,   default=lambda: datetime.now(timezone.utc))   # When the session was started
    updated_at = db.Column(db.DateTime,   default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))
    # updated_at is refreshed automatically whenever the row is modified
    # Used to sort sessions by recency and calculate the 30-day expiry

    messages = db.relationship("ChatMessage", backref="session", lazy=True, cascade="all, delete-orphan")
    # Links this session to its ChatMessage rows — deleting the session deletes all messages too

    def to_dict(self, include_messages=False):
        # Converts the session to a dict for JSON responses
        d = {
            "id":            self.id,
            "title":         self.title,
            "created_at":    self.created_at.isoformat() if self.created_at else None,
            "updated_at":    self.updated_at.isoformat() if self.updated_at else None,
            "message_count": len(self.messages)  # How many messages are in this session
        }
        if include_messages:
            d["messages"] = [m.to_dict() for m in self.messages]  # Include full message list only when loading a session
        return d


class ChatMessage(db.Model):
    __tablename__ = "chat_messages"  # SQL table name

    id         = db.Column(db.Integer,    primary_key=True, autoincrement=True)  # Auto-numbered integer ID
    session_id = db.Column(db.Integer,   db.ForeignKey("chat_sessions.id"), nullable=False)  # Which session this message belongs to
    role       = db.Column(db.String(20), nullable=False)   # "user" or "assistant"
    content    = db.Column(db.Text,       nullable=False)   # The actual message text (no length limit)
    source     = db.Column(db.String(50), nullable=True)    # Which system answered: "gemini", "claude", "question_bank", etc.
    created_at = db.Column(db.DateTime,  default=lambda: datetime.now(timezone.utc))  # Timestamp

    def to_dict(self):
        # Converts the message to a dict for JSON responses
        return {
            "id":         self.id,
            "role":       self.role,
            "content":    self.content,
            "source":     self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


class QuestionBankEntry(db.Model):
    """
    Stores every QB topic in the database instead of hardcoding them in the Python dict.
    This means entries can be added, edited, or deleted at runtime without touching the code.
    """
    __tablename__ = "question_bank"

    id       = db.Column(db.Integer,    primary_key=True, autoincrement=True)
    key      = db.Column(db.String(200), unique=True, nullable=False, index=True)
    # key is the topic phrase used for matching (e.g. "sql injection", "packet sniffing tool")
    # index=True speeds up lookups since search_qb queries by key

    answer   = db.Column(db.Text,       nullable=False)    # The full response text (markdown)
    severity = db.Column(db.String(20), nullable=False, default="info")  # "critical", "high", or "info"
    tags     = db.Column(db.Text,       nullable=False, default="[]")
    # tags stored as a JSON string (e.g. '["web", "owasp"]') — SQLite has no native array type

    def tags_list(self):
        """Return tags as a Python list."""
        import json
        try:
            return json.loads(self.tags)
        except Exception:
            return []

    def to_dict(self):
        return {
            "key":      self.key,
            "answer":   self.answer,
            "severity": self.severity,
            "tags":     self.tags_list()
        }



# ── Database Initialisation ───────────────────────────────────────────────────

def purge_old_sessions():
    """Delete any chat sessions not updated in the last 30 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)  # Calculate the expiry date
    old = ChatSession.query.filter(ChatSession.updated_at < cutoff).all()  # Find all expired sessions
    for s in old:
        db.session.delete(s)   # Queue each one for deletion
    if old:
        db.session.commit()    # Write the deletions to the database
        logger.info(f"✓ Purged {len(old)} chat session(s) older than 30 days")

with app.app_context():
    # app_context() is required for any DB operation that runs outside a request
    db.create_all()          # Create any tables that don't exist yet — safe to call every time, skips existing tables
    purge_old_sessions()     # Clean up expired sessions on every server start

    # QB entries live in the database — added once via seed_qb.py or DB Browser
    qb_count = QuestionBankEntry.query.count()
    logger.info(f"✓ QB loaded from database ({qb_count} entries)")

    logger.info("✓ Database ready: cybershield.db")


# ── Password Hashing ──────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Turn a plain password into a bcrypt hash for safe storage."""
    return bcrypt.hashpw(
        password.encode(),       # Convert the string to bytes — bcrypt requires bytes
        bcrypt.gensalt(rounds=12)  # Generate a unique random salt; rounds=12 means 4096 iterations (slow enough to resist brute force)
    ).decode()                   # Convert the resulting bytes back to a string for database storage

def verify_password(password: str, hashed: str) -> bool:
    """Check a plain password against a stored bcrypt hash."""
    try:
        return bcrypt.checkpw(
            password.encode(),  # Convert input to bytes
            hashed.encode()     # Convert stored hash to bytes
        )
        # bcrypt.checkpw extracts the salt from the stored hash, re-hashes the input with it,
        # then compares — the comparison is constant-time to prevent timing attacks
    except Exception:
        return False  # Return False if the stored hash is malformed instead of crashing

def generate_password(length: int = 16,
                      use_upper: bool = True,
                      use_lower: bool = True,
                      use_digits: bool = True,
                      use_symbols: bool = True) -> dict:
    """
    Generate a cryptographically secure random password.

    Character pools mirror the HTML generator exactly:
      UPPER   — A-Z excluding I and O (look like 1 and 0)
      LOWER   — a-z excluding i, l, and o
      DIGITS  — 2-9 (no 0 or 1)
      SYMBOLS — !@#$%&*-+? (common, found on every keyboard)

    Strategy: guarantee at least one character from every active pool,
    fill the remainder from the combined charset, then Fisher-Yates shuffle
    the whole result so no positional patterns remain.

    Returns a dict with:
      password — the generated string
      entropy_bits — approximate Shannon entropy (float)
      strength — "Fair" / "Good" / "Strong" / "Excellent"
    """
    import math

    UPPER   = "ABCDEFGHJKLMNPQRSTUVWXYZ"   # I, O removed
    LOWER   = "abcdefghjkmnpqrstuvwxyz"    # i, l, o removed
    DIGITS  = "23456789"                   # 0, 1 removed
    SYMBOLS = "!@#$%&*-+?"

    pools = []
    if use_upper:   pools.append(UPPER)
    if use_lower:   pools.append(LOWER)
    if use_digits:  pools.append(DIGITS)
    if use_symbols: pools.append(SYMBOLS)

    if not pools:
        raise ValueError("At least one character pool must be selected")

    length = max(6, min(128, length))  # Clamp: 6 ≤ length ≤ 128

    charset = "".join(pools)

    # Guarantee one character from each active pool
    guaranteed = [secrets.choice(pool) for pool in pools]

    # Fill remaining slots from the combined charset
    remaining = [secrets.choice(charset) for _ in range(length - len(guaranteed))]

    # Merge and shuffle — breaks any positional pattern from the guarantee step
    all_chars = guaranteed + remaining
    for i in range(len(all_chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        all_chars[i], all_chars[j] = all_chars[j], all_chars[i]

    password = "".join(all_chars)

    # Shannon entropy: H = length × log2(charset_size)
    entropy_bits = round(length * math.log2(len(charset)), 1)
    if entropy_bits > 100:
        strength = "Excellent"
    elif entropy_bits > 75:
        strength = "Strong"
    elif entropy_bits > 50:
        strength = "Good"
    else:
        strength = "Fair"

    return {
        "password":     password,
        "entropy_bits": entropy_bits,
        "strength":     strength,
    }


# ── API Keys ──────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")  # Claude API key — empty string if not in .env
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")     # Gemini API key
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")     # OpenAI API key
SERPER_API_KEY    = os.getenv("SERPER_API_KEY", "")     # Serper (Google search) API key
# If any key is an empty string, that AI provider is skipped and the next one is tried



# ── QB Search ─────────────────────────────────────────────────────────────────

def search_qb(query: str) -> dict | None:
    """
    Decide whether the query should be answered from the QB (database) or sent to AI.
    Returns a result dict {answer, severity, tags} or None to let AI handle it.
    """
    q = query.lower().strip()

    # If the query sounds conversational or asks for advice, skip QB and let AI answer
    CONVERSATIONAL_PREFIXES = (
        "suggest", "give me", "recommend", "advise", "advice",
        "how do i", "how to", "how can i", "how should i",
        "what are", "what is the best", "what steps", "steps for",
        "tips for", "best way", "improve", "strengthen", "change my",
        "create a new", "make a new", "i need a new", "help me",
        "can you", "could you", "please", "i want to",
    )
    for prefix in CONVERSATIONAL_PREFIXES:
        if q.startswith(prefix) or f" {prefix} " in q:
            return None  # Route to AI instead

    # Check if the user is asking for starter code
    starter_triggers = ("starter code", "show me", "starter", "example code",
                        "sample code", "project for", "code for", "write a")
    is_starter_request = any(t in q for t in starter_triggers)

    q_words = set(re.findall(r'\w+', q))  # All individual words in the query

    # Query the database — all QB entries now live in the question_bank table
    all_entries = QuestionBankEntry.query.all()

    for entry in all_entries:
        key       = entry.key
        key_words = set(re.findall(r'\w+', key))

        if is_starter_request:
            if key in q:  # Exact key phrase must appear in the query
                return {"answer": entry.answer, "severity": entry.severity, "tags": entry.tags_list()}
            continue

        # All key words must appear in the query
        if not key_words.issubset(q_words):
            continue

        # Reject if diluting words shift the meaning away from a plain definition lookup
        DILUTING_WORDS = {
            "suggest", "new", "better", "stronger", "change", "reset",
            "forget", "forgot", "steps", "tips", "advice", "improve",
            "best", "good", "create", "make", "different", "another",
            "generate", "help", "please", "need", "want",
        }
        extra_words = q_words - key_words - {"a","an","the","is","what","about","tell","me","i","my","for","of","with"}
        if len(extra_words & DILUTING_WORDS) > 0:
            return None

        if key in q:  # The exact key phrase must appear as a substring
            return {"answer": entry.answer, "severity": entry.severity, "tags": entry.tags_list()}

    return None  # Nothing matched — send to AI


# ── System Prompt ─────────────────────────────────────────────────────────────

# This string is sent to every AI model as the system/role instruction
# It tells the AI to act as a cybersecurity expert and how to format replies
SYSTEM_PROMPT = """You are CyberGuard AI, an expert cybersecurity assistant. You specialize in:
- Vulnerability analysis and CVEs
- Penetration testing techniques and tools
- Security best practices and hardening
- Incident response and forensics
- Cryptography and network security
- Malware analysis and threat intelligence
- Compliance frameworks (ISO 27001, NIST, SOC2, PCI-DSS)

Always provide accurate, actionable, and educational information. Format your responses with clear structure using markdown. For code examples, use code blocks. Flag anything that could be misused with ethical warnings. Keep answers focused, intermediate-level, and practical."""


# ── AI Provider Functions ─────────────────────────────────────────────────────

def ask_claude(messages: list, question: str) -> str:
    """Send the conversation to Claude and return its reply."""
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set")  # Triggers fallback to next AI

    payload = {
        "model": "claude-opus-4-5",
        "max_tokens": 1500,         # Cap the response length
        "system": SYSTEM_PROMPT,    # The role/behavior instruction
        "messages": messages        # Full conversation history
    }
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",  # Required header — Anthropic API version
            "content-type": "application/json"
        },
        json=payload, timeout=30    # Give up after 30 seconds so the server doesn't hang
    )
    resp.raise_for_status()         # Raise an exception on HTTP 4xx/5xx errors
    return resp.json()["content"][0]["text"]  # Extract the text from the first content block


def ask_gemini(question: str) -> str:
    """Send the question to Gemini and return its reply."""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not set")

    prompt = f"{SYSTEM_PROMPT}\n\nUser question: {question}"
    # Gemini doesn't support multi-turn history in this integration, so we prepend the system prompt
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}",
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=30
    )
    resp.raise_for_status()
    candidates = resp.json().get("candidates", [])  # Gemini returns a list of candidate responses
    if candidates:
        return candidates[0]["content"]["parts"][0]["text"]  # Take the first candidate's text
    raise ValueError("Empty Gemini response")  # Triggers fallback if Gemini returned nothing


def ask_openai(messages: list) -> str:
    """Send the conversation to OpenAI GPT-4o-mini and return its reply."""
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY not set")

    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        # System prompt is inserted as the first message with role "system"
        "max_tokens": 1500
    }
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload, timeout=30
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]  # Extract the assistant's reply


def web_search(query: str) -> str:
    """Search Google via Serper and return formatted results as a string."""
    if not SERPER_API_KEY:
        raise ValueError("SERPER_API_KEY not set")

    resp = requests.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
        json={"q": f"cybersecurity {query}", "num": 5},  # Prepend "cybersecurity" to bias results
        timeout=15
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for item in data.get("organic", [])[:4]:  # Take top 4 organic search results
        results.append(f"**{item.get('title')}**\n{item.get('snippet')}\n🔗 {item.get('link')}")
    return "\n\n".join(results) if results else "No results found."


# ── Query Router ──────────────────────────────────────────────────────────────

def _build_ai_order(preferred: str) -> list:
    """Put the user's preferred AI first, then the others in default order."""
    all_ai = ["claude", "gemini", "openai"]
    order = [preferred] if preferred in all_ai else []
    for ai in all_ai:
        if ai not in order:
            order.append(ai)  # Add any AIs not already in the list
    return order


def route_query(question: str, history: list, preferred_ai: str) -> dict:
    """
    Main decision function. Called for every chat message.
    Tries sources in order: QB → preferred AI → other AIs → web search → error.
    Always returns a dict with: answer, source, severity, tags, sources_tried.
    """
    sources_tried = []

    # Step 1: Check the local question bank first — instant, free, offline
    qb_result = search_qb(question)
    if qb_result:
        return {
            "answer":       qb_result["answer"],
            "source":       "question_bank",
            "severity":     qb_result.get("severity", "info"),
            "tags":         qb_result.get("tags", []),
            "sources_tried": ["question_bank"]
        }

    # Step 2: Build the conversation history for AI context (last 6 exchanges to keep payload small)
    ai_messages = [{"role": m["role"], "content": m["content"]} for m in history[-6:]]
    ai_messages.append({"role": "user", "content": question})  # Add the current question

    # Step 3: Try each AI in priority order (preferred first, then fallbacks)
    ai_order = _build_ai_order(preferred_ai)
    for ai_name in ai_order:
        sources_tried.append(ai_name)
        try:
            if ai_name == "claude":
                answer = ask_claude(ai_messages, question)
            elif ai_name == "gemini":
                answer = ask_gemini(question)
            elif ai_name == "openai":
                answer = ask_openai(ai_messages)
            else:
                continue
            return {
                "answer":        answer,
                "source":        ai_name,
                "severity":      "info",
                "tags":          [],
                "sources_tried": sources_tried
            }
        except Exception as e:
            logger.warning(f"{ai_name} failed: {e}")  # Log the failure and try the next AI

    # Step 4: All AIs failed — try a web search as a last resort
    sources_tried.append("web_search")
    try:
        web_result = web_search(question)
        return {
            "answer":        f"*No AI model was available, here are web results:*\n\n{web_result}",
            "source":        "web_search",
            "severity":      "info",
            "tags":          [],
            "sources_tried": sources_tried
        }
    except Exception as e:
        logger.error(f"Web search failed: {e}")

    # Step 5: Everything failed — return a friendly error
    return {
        "answer":        "⚠️ All sources are currently unavailable. Please check your API keys in the `.env` file and try again.",
        "source":        "error",
        "severity":      "info",
        "tags":          [],
        "sources_tried": sources_tried
    }


# ── Page Routes ───────────────────────────────────────────────────────────────
# These serve the HTML files when the browser navigates to these URLs

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/Login.html")
def login_page():
    return render_template("Login.html")

@app.route("/Signup.html")
def signup_page():
    return render_template("Signup.html")

@app.route("/Profile.html")
def profile_page():
    return render_template("Profile.html")


# ── Auth Routes ───────────────────────────────────────────────────────────────

@app.route("/api/generate-password", methods=["GET"])
def generate_password_route():
    """
    Return a freshly generated secure password.
    Optional query params (all match the HTML generator defaults):
      length   — integer 6-128 (default 16)
      upper    — "true"/"false" (default true)
      lower    — "true"/"false" (default true)
      digits   — "true"/"false" (default true)
      symbols  — "true"/"false" (default true)

    Example: GET /api/generate-password?length=20&symbols=false
    """
    def bool_param(name: str, default: bool = True) -> bool:
        val = request.args.get(name, "").lower()
        if val == "false": return False
        if val == "true":  return True
        return default

    try:
        length      = int(request.args.get("length", 16))
        use_upper   = bool_param("upper")
        use_lower   = bool_param("lower")
        use_digits  = bool_param("digits")
        use_symbols = bool_param("symbols")

        result = generate_password(
            length=length,
            use_upper=use_upper,
            use_lower=use_lower,
            use_digits=use_digits,
            use_symbols=use_symbols,
        )
        return jsonify({"success": True, **result})

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/signup", methods=["POST"])
def signup():
    data     = request.json or {}
    email    = (data.get("email") or "").strip().lower()    # Normalize email to lowercase
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    # Validate inputs before touching the database
    if not email or "@" not in email:
        return jsonify({"success": False, "error": "Valid email required"}), 400
    if len(username) < 3:
        return jsonify({"success": False, "error": "Username must be 3+ characters"}), 400
    if len(password) < 6:
        return jsonify({"success": False, "error": "Password must be 6+ characters"}), 400

    # Check for duplicates — 409 Conflict is the correct HTTP status for "already exists"
    if User.query.filter_by(username=username).first():
        return jsonify({"success": False, "error": "Username already taken"}), 409
    if User.query.filter_by(email=email).first():
        return jsonify({"success": False, "error": "Email already registered"}), 409

    user = User(
        email=email,
        username=username,
        password_hash=hash_password(password)  # Store bcrypt hash, never the plain password
    )
    db.session.add(user)   # Stage the new user for insertion
    db.session.commit()    # Write to the database

    session["user_id"] = user.id  # Log them in immediately after registering
    return jsonify({"success": True, "username": username})


@app.route("/api/login", methods=["POST"])
def login():
    data     = request.json or {}
    email    = (data.get("email") or "").strip().lower()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    # Look up the user by username (case-insensitive comparison)
    user = User.query.filter(
        db.func.lower(User.username) == username.lower()
    ).first()

    if not user or not verify_password(password, user.password_hash):
        return jsonify({"success": False, "error": "Invalid credentials"}), 401
        # Return the same error for both "user not found" and "wrong password"
        # This prevents attackers from knowing which one was wrong (username enumeration)

    if email and user.email != email:
        # Also verify email if it was provided — extra check
        return jsonify({"success": False, "error": "Invalid credentials"}), 401

    session["user_id"] = user.id  # Store user ID in the session cookie — they're now logged in
    return jsonify({"success": True, "username": user.username})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()  # Remove all session data — the user is now unauthenticated
    return jsonify({"success": True})


@app.route("/api/me", methods=["GET"])
def me():
    """Return the current logged-in user's info, or an empty object if not logged in."""
    uid = session.get("user_id")  # Read the user ID from the session cookie
    if not uid:
        return jsonify({})  # Empty object signals "not logged in" to the frontend

    user = User.query.get(uid)
    if not user:
        # User ID is in the session but the account no longer exists
        session.clear()  # Clear the stale session
        return jsonify({})

    return jsonify({
        "username":   user.username,
        "email":      user.email,
        "id":         user.id,
        "created_at": user.created_at.isoformat() if user.created_at else None
    })


# ── Profile Update Route ──────────────────────────────────────────────────────

@app.route("/api/profile/update", methods=["POST"])
def profile_update():
    """Handle all profile changes: email, username, password, and account deletion."""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    user = User.query.get(uid)
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    data        = request.json or {}
    update_type = data.get("type")  # What the user wants to change

    if update_type == "email":
        new_email   = (data.get("email") or "").strip().lower()
        current_pwd = data.get("current_password") or ""
        if not new_email or "@" not in new_email:
            return jsonify({"success": False, "error": "Valid email required"}), 400
        if not verify_password(current_pwd, user.password_hash):
            return jsonify({"success": False, "error": "Password incorrect"}), 403  # 403 Forbidden
        existing = User.query.filter_by(email=new_email).first()
        if existing and existing.id != uid:
            return jsonify({"success": False, "error": "Email already in use"}), 409
        user.email = new_email  # Update the field in memory

    elif update_type == "username":
        new_username = (data.get("username") or "").strip()
        current_pwd  = data.get("current_password") or ""
        if len(new_username) < 3:
            return jsonify({"success": False, "error": "Username must be 3+ characters"}), 400
        if not verify_password(current_pwd, user.password_hash):
            return jsonify({"success": False, "error": "Password incorrect"}), 403
        existing = User.query.filter(
            db.func.lower(User.username) == new_username.lower()
        ).first()
        if existing and existing.id != uid:
            return jsonify({"success": False, "error": "Username already taken"}), 409
        user.username = new_username

    elif update_type == "password":
        current_pwd = data.get("current_password") or ""
        new_pwd     = data.get("new_password") or ""
        if not verify_password(current_pwd, user.password_hash):
            return jsonify({"success": False, "error": "Current password incorrect"}), 403
        if len(new_pwd) < 6:
            return jsonify({"success": False, "error": "New password must be 6+ characters"}), 400
        user.password_hash = hash_password(new_pwd)  # Re-hash the new password

    elif update_type == "delete_account":
        db.session.delete(user)  # This also deletes all their sessions and messages (cascade)
        db.session.commit()
        session.clear()          # Log them out immediately
        return jsonify({"success": True, "message": "Account deleted"})

    else:
        return jsonify({"success": False, "error": "Unknown update type"}), 400

    db.session.commit()  # Write all in-memory changes to the database
    return jsonify({
        "success": True,
        "message": f"{update_type.capitalize()} updated successfully",
        "user":    {"username": user.username, "email": user.email}  # Return updated values to the frontend
    })


# ── Chat Route ────────────────────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
def chat():
    """Receive a user message, get an answer, save to DB if logged in, and return the result."""
    data       = request.json or {}
    question   = (data.get("message") or "").strip()
    history    = data.get("history", [])           # Previous messages for AI context
    preferred  = data.get("preferred_ai", "gemini") # Which AI to try first
    session_id = data.get("session_id")             # Existing session ID if continuing a conversation

    if not question:
        return jsonify({"error": "Empty message"}), 400

    start  = time.time()
    result = route_query(question, history, preferred)  # Get the answer from QB or AI
    result["response_time"] = round(time.time() - start, 2)  # How long it took in seconds
    result["timestamp"]     = datetime.now(timezone.utc).isoformat()

    uid = session.get("user_id")  # Check if the user is logged in
    if uid:
        user = User.query.get(uid)
        if user:
            chat_session = None

            if session_id:
                # Try to find the existing session — makes sure it belongs to this user
                chat_session = ChatSession.query.filter_by(id=session_id, user_id=uid).first()

            if not chat_session:
                # No existing session — create a new one
                title = question[:60] + ("..." if len(question) > 60 else "")  # First 60 chars as title
                chat_session = ChatSession(user_id=uid, title=title)
                db.session.add(chat_session)
                db.session.flush()  # Write to DB temporarily to get the auto-generated ID without full commit

            # Save the user's message to the database
            user_msg = ChatMessage(
                session_id=chat_session.id,
                role="user",
                content=question,
                source="user"
            )
            db.session.add(user_msg)

            # Save the AI's response to the database
            ai_msg = ChatMessage(
                session_id=chat_session.id,
                role="assistant",
                content=result["answer"],
                source=result.get("source", "unknown")  # Which AI or source produced this
            )
            db.session.add(ai_msg)

            chat_session.updated_at = datetime.now(timezone.utc)  # Mark the session as recently active
            db.session.commit()  # Write all staged changes to the database in one transaction

            result["session_id"] = chat_session.id  # Tell the frontend which session this belongs to

    return jsonify(result)


# ── History Routes ────────────────────────────────────────────────────────────

@app.route("/api/history", methods=["GET"])
def get_history():
    """Return the user's list of chat sessions, newest first. Purge expired ones first."""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    # Delete sessions older than 30 days before returning the list
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    old = ChatSession.query.filter(
        ChatSession.user_id == uid,
        ChatSession.updated_at < cutoff
    ).all()
    for s in old:
        db.session.delete(s)
    if old:
        db.session.commit()

    sessions = ChatSession.query.filter_by(user_id=uid)\
                                .order_by(ChatSession.updated_at.desc())\
                                .limit(100).all()  # Newest first, max 100
    return jsonify({
        "success":  True,
        "sessions": [s.to_dict() for s in sessions]  # to_dict() without messages (just metadata)
    })


@app.route("/api/history/<int:session_id>", methods=["GET"])
def get_session(session_id):
    """Return a single session including all its messages — used when loading a conversation."""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    chat_session = ChatSession.query.filter_by(id=session_id, user_id=uid).first()
    # filter_by user_id ensures users can only access their own sessions
    if not chat_session:
        return jsonify({"success": False, "error": "Session not found"}), 404

    return jsonify({
        "success": True,
        "session": chat_session.to_dict(include_messages=True)  # Include full message list
    })


@app.route("/api/history/<int:session_id>", methods=["DELETE"])
def delete_session(session_id):
    """Delete a single chat session and all its messages."""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    chat_session = ChatSession.query.filter_by(id=session_id, user_id=uid).first()
    if not chat_session:
        return jsonify({"success": False, "error": "Session not found"}), 404

    db.session.delete(chat_session)  # cascade="all, delete-orphan" removes messages too
    db.session.commit()
    return jsonify({"success": True})


@app.route("/api/history/<int:session_id>/rename", methods=["PATCH"])
def rename_session(session_id):
    """Update the title of a chat session."""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    chat_session = ChatSession.query.filter_by(id=session_id, user_id=uid).first()
    if not chat_session:
        return jsonify({"success": False, "error": "Session not found"}), 404

    data      = request.json or {}
    new_title = (data.get("title") or "").strip()[:100]  # Cap at 100 characters
    if not new_title:
        return jsonify({"success": False, "error": "Title cannot be empty"}), 400

    chat_session.title = new_title
    db.session.commit()
    return jsonify({"success": True})


@app.route("/api/history/clear", methods=["DELETE"])
def clear_history():
    """Delete all chat sessions for the logged-in user."""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    ChatSession.query.filter_by(user_id=uid).delete()  # Bulk delete — cascade removes all messages
    db.session.commit()
    return jsonify({"success": True, "message": "All chat history cleared"})


# ── Data Routes ───────────────────────────────────────────────────────────────

@app.route("/api/starter-codes", methods=["GET"])
def starter_codes():
    """Return the list of available project ideas for the Projects sidebar tab."""
    projects = [
        # Each entry: key (matches QB key), title (display name), description (short summary)
        {"key": "packet sniffing tool",           "title": "Packet Sniffer",              "description": "Monitor and capture network packets"},
        {"key": "keylogger project",              "title": "Keylogger",                   "description": "Record keystrokes with timestamps"},
        {"key": "caesar cipher",                  "title": "Caesar Cipher",               "description": "Encrypt/decrypt with Caesar cipher"},
        {"key": "hash function implementation",   "title": "Hash Function Tool",          "description": "Generate MD5, SHA-256 and more"},
        {"key": "sql injection vulnerability scanner", "title": "SQL Injection Scanner",  "description": "Detect SQLi in web apps"},
        {"key": "credit card fraud detection",    "title": "Fraud Detection ML",          "description": "ML model using scikit-learn"},
        {"key": "internet border patrol",         "title": "Internet Border Patrol",      "description": "Monitor traffic for suspicious activity"},
        {"key": "password generator",             "title": "Password Generator",          "description": "Generate strong unique passwords"},
        {"key": "file encryption decryption",     "title": "File Encryption",             "description": "Encrypt files using AES-128 Fernet"},
        {"key": "website vulnerability scanner",  "title": "Website Scanner",            "description": "Scan HTML for security weaknesses"},
        {"key": "network scanner lite",           "title": "Network Scanner (Lite)",      "description": "Discover devices on local network"},
        {"key": "hash function explorer",         "title": "Hash Function Explorer",      "description": "Explore hash properties and avalanche effect"},
        {"key": "data privacy visualizer",        "title": "Data Privacy Visualizer",     "description": "Chart how apps collect your data"},
        {"key": "secure password vault",          "title": "Secure Password Vault",       "description": "Encrypted local password manager"},
        {"key": "password crasher checker",       "title": "Password Crasher/Checker",    "description": "Crack weak hashes and check strength"},
        {"key": "web scraper security",           "title": "Security Web Scraper",        "description": "Scrape sites for vulnerability patterns"},
        {"key": "social engineering simulator",   "title": "Phishing Simulator",          "description": "Phishing awareness training GUI"},
        {"key": "data encryption decryption tool","title": "AES + RSA Encryption Tool",   "description": "AES-256 and RSA encrypt/decrypt"},
        {"key": "brute force attack simulator",   "title": "Brute-Force Simulator",       "description": "Simulate attacks and rate-limit defenses"},
        {"key": "malware analysis tool",          "title": "Malware Analysis Tool",       "description": "Static analysis of suspicious files"},
        {"key": "cybersecurity escape room",      "title": "Cybersecurity Escape Room",   "description": "CLI puzzle game with crypto challenges"},
        {"key": "malware analysis sandbox",       "title": "Malware Sandbox",             "description": "Safe behavioral analysis environment"},
        {"key": "secure chat application",        "title": "Secure Chat App",             "description": "End-to-end encrypted chat"},
        {"key": "threat intelligence dashboard",  "title": "Threat Intel Dashboard",      "description": "Aggregate VirusTotal and AbuseIPDB feeds"},
        {"key": "honeypot",                       "title": "Honeypot Deployment",         "description": "Decoy system to trap attackers"},
        {"key": "automated vulnerability scanner","title": "Auto Vuln Scanner",           "description": "Crawl and test for XSS and SQLi"},
        {"key": "secure file sharing",            "title": "Secure File Sharing",         "description": "Encrypted file transfer with checksums"},
        {"key": "ctf challenge",                  "title": "CTF Competition",             "description": "Create your own Capture The Flag"},
        {"key": "cybersecurity awareness game",   "title": "Awareness Quiz Game",         "description": "Interactive cybersecurity quiz"},
    ]
    return jsonify({"projects": projects})


@app.route("/api/status", methods=["GET"])
def status():
    """Return which API keys are configured — the frontend uses this to light up status dots."""
    return jsonify({
        "claude":     bool(ANTHROPIC_API_KEY),
        "gemini":     bool(GEMINI_API_KEY),
        "openai":     bool(OPENAI_API_KEY),
        "web_search": bool(SERPER_API_KEY),
        "qb_entries": QuestionBankEntry.query.count(),  # Live count from the database
        "database":   "sqlite"
    })


@app.route("/api/topics", methods=["GET"])
def topics():
    """Return all QB keys with their tags and severity — used to render the Topics sidebar tab."""
    entries = QuestionBankEntry.query.all()  # Read directly from the database
    return jsonify({
        "topics": [
            {"key": e.key, "tags": e.tags_list(), "severity": e.severity}
            for e in entries
        ]
    })


# ── Entry Point ───────────────────────────────────────────────────────────────

def start_flask():
    """Start Flask in a background thread so pywebview can launch on the main thread."""
    try:
        from waitress import serve
        logger.info("✓ Flask server starting on http://127.0.0.1:5000")
        serve(app, host="127.0.0.1", port=5000)
    except ImportError:
        logger.warning("Waitress not installed — falling back to Flask dev server")
        app.run(debug=False, port=5000, use_reloader=False)
        # use_reloader=False is required here — the reloader spawns a second process
        # which breaks the pywebview window (it would open twice)


if __name__ == "__main__":
    try:
        import webview
        import threading
        import time

        # Flask must start before pywebview tries to load the URL, so we give it
        # a moment to bind the port before creating the window
        flask_thread = threading.Thread(target=start_flask, daemon=True)
        # daemon=True means the thread dies automatically when the window is closed —
        # no need to manually shut down Flask
        flask_thread.start()
        time.sleep(1)       # give Flask ~1 second to finish binding to port 5000

        logger.info("✓ Launching CyberShield desktop window")
        window = webview.create_window(
            title     = "CyberShield",           # title bar text
            url       = "http://127.0.0.1:5000", # points at our running Flask server
            width     = 1280,                    # good default for the chat + sidebar layout
            height    = 820,
            min_size  = (900, 600),              # prevent the window from becoming unusable if resized small
            resizable = True,
        )
        webview.start()     # blocks here — hands control to the OS window loop
        # when the user closes the window, webview.start() returns and the process exits cleanly

    except ImportError:
        # pywebview not installed — auto-open the browser and start Flask
        import threading, webbrowser, time
        logger.warning("pywebview not installed — opening in browser instead")
        logger.info("✓ Starting CyberShield at http://127.0.0.1:5000")

        def open_browser():
            time.sleep(1.2)   # wait for Flask to bind before opening the tab
            webbrowser.open("http://127.0.0.1:5000")

        threading.Thread(target=open_browser, daemon=True).start()
        start_flask()   # blocks on the main thread while Flask runs
