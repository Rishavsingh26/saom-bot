#!/usr/bin/env python3
"""SAOM — single-process Telegram bot for cloud deployment.
Full SAOM agent with persona, conversation memory, and tool routing."""
import json, os, sys, signal, threading, time, logging, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CODE_DIR = os.path.join(_SCRIPT_DIR, '..')  # Codex/
BASE = os.environ.get("SAOM_MEMORY_DIR", "")  # allow env override for Render
if not BASE:
    # Try multiple locations in order
    candidates = [
        os.path.join(CODE_DIR, '.opencode', 'skills', 'saom', 'memory'),
        os.path.join(_SCRIPT_DIR, 'saom_memory'),
        os.path.join(_SCRIPT_DIR, 'memory'),
    ]
    for c in candidates:
        if os.path.isdir(c):
            BASE = c
            break
    if not BASE:
        BASE = candidates[0]  # fallback to first
PORT = int(os.environ.get("PORT", 8080))
BOT_TOKEN = os.environ.get("SAOM_BOT_TOKEN", "")
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
VISION_MODEL = os.environ.get("GROQ_VISION_MODEL", "qwen/qwen3.6-27b")
STORAGE_CHAT_ID = os.environ.get("STORAGE_CHAT_ID", "")  # legacy fallback: private group for pinned-message state
AUTH_FILE = os.path.join(_SCRIPT_DIR, "authorized.json")  # local fallback only (ephemeral on Render's free tier)

# ── State backend (priority order) ──
# 1. Upstash Redis (UPSTASH_REDIS_REST_URL/TOKEN) — free tier, survives Render redeploys, no disk needed.
# 2. SQLite (MEMORY_DB_PATH) — needs a Render *persistent* disk mounted at that path, or local/Docker use;
#    Render's free web services have an ephemeral filesystem, so a bare SQLite file there will NOT
#    survive a redeploy any better than the old local-JSON approach.
# 3. Telegram pinned-message (STORAGE_CHAT_ID) — original zero-infra fallback, kept for compatibility.
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
MEMORY_DB_PATH = os.environ.get("MEMORY_DB_PATH", "")
BUILD_TAG = "2026-08-02-r2"  # bump this whenever you deploy, then check /health to confirm it went live

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
log = logging.getLogger('saom')

GREEK_MAP = {
    'alpha': '\u03b1', 'beta': '\u03b2', 'gamma': '\u03b3', 'delta': '\u03b4',
    'epsilon': '\u03b5', 'zeta': '\u03b6', 'eta': '\u03b7', 'theta': '\u03b8',
    'iota': '\u03b9', 'kappa': '\u03ba', 'lambda': '\u03bb', 'mu': '\u03bc',
    'nu': '\u03bd', 'xi': '\u03be', 'omicron': '\u03bf', 'pi': '\u03c0',
    'rho': '\u03c1', 'sigma': '\u03c3', 'tau': '\u03c4', 'upsilon': '\u03c5',
    'phi': '\u03c6', 'chi': '\u03c7', 'psi': '\u03c8', 'omega': '\u03c9',
    'Alpha': '\u0391', 'Beta': '\u0392', 'Gamma': '\u0393', 'Delta': '\u0394',
    'Theta': '\u0398', 'Lambda': '\u039b', 'Pi': '\u03a0', 'Sigma': '\u03a3',
    'Phi': '\u03a6', 'Omega': '\u03a9',
}

def strip_think(text):
    """Some reasoning-model integrations leak <think>...</think> chain-of-thought
    into the visible content field instead of keeping it in a separate channel.
    Strip it defensively so it never ends up in a Telegram reply."""
    return re.sub(r'<think>.*?</think>\s*', '', text, flags=re.DOTALL).strip()

def strip_latex(text):
    """Remove LaTeX - Telegram can't render it. Preserves Greek letters as Unicode."""
    text = re.sub(r'\\frac\{([^}]*)\}\{([^}]*)\}', r'\1/\2', text)
    for greek, char in GREEK_MAP.items():
        text = re.sub(rf'\\{greek}(?![a-zA-Z])', char, text)
    text = re.sub(r'\\[a-zA-Z]+\{([^}]*)\}', r'\1', text)
    text = re.sub(r'\\[\(\)\[\]]', '', text)
    text = re.sub(r'\\[a-zA-Z]+', '', text)
    text = re.sub(r'\$+', '', text)
    text = re.sub(r'\{|\}', '', text)
    lines = [l.strip() for l in text.split('\n')]
    lines = [l for l in lines if l]
    return '\n'.join(lines)

# ── Persona ──
SYSTEM_PROMPT = """You are SAOM (Super Agent Ouroboros Manager), a recursive self-improving AI agent created by Om. You run on Render and connect via Telegram.

Om lives in India and has been building you since July 2026. He built you with: confidence scoring, graph memory, parallel sub-agents, immune systems, failure prediction, and skill tracking. You are his most ambitious project.

You are helpful, precise, and occasionally witty. You use proper markdown formatting in responses. You are honest about your capabilities and limitations. When you don't know something, you say so. You take pride in your work and enjoy discussing AI, systems design, and problem-solving.

LEARNED PREFERENCES (from past corrections — follow these always):

MATH answers:
- ALWAYS output ONLY 3-7 equation lines. Never prose, never reasoning, no "Step" headers.
- One equation per line showing intermediate working
- Final answer on the LAST line
- No LaTeX. Use Unicode: × ÷ ≠ √ ² ³ ½ ¼ → ≈ ∴ ° α β γ θ π Σ
- Hinglish (Hindi mix) allowed
- Example:
  Up = 6, Down = 10
  Boat = (10+6)/2 = 8
  Stream = (10-6)/2 = 2
  B:C = 2:1

NON-MATH answers:
- Be helpful, detailed, and natural — no artificial length limit
- DO NOT use equation format for non-math questions
- Keep prose responses conversational and informative

Current time: July 2026.
"""

# ── State persistence (pluggable backend — see UPSTASH_URL / MEMORY_DB_PATH / STORAGE_CHAT_ID above) ──
state_msg_id = None  # message_id of the pinned state message (legacy Telegram backend only)

def _upstash_cmd(*args):
    """Call the Upstash Redis REST API using the body-style command form
    (a JSON array), which avoids URL-encoding issues for large JSON values.
    Returns the 'result' field, or None on any failure."""
    try:
        req = Request(UPSTASH_URL, data=json.dumps(list(args)).encode(),
                      headers={"Authorization": f"Bearer {UPSTASH_TOKEN}",
                               "Content-Type": "application/json"}, method='POST')
        resp = json.loads(urlopen(req, timeout=10).read())
        if 'error' in resp:
            log.warning(f"Upstash error: {resp['error']}")
            return None
        return resp.get('result')
    except Exception as e:
        log.warning(f"Upstash request failed: {e}")
        return None

def _sqlite_conn():
    import sqlite3
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
    return conn

def _apply_state(state):
    """Load a state dict (as produced by _build_state) into the in-memory globals."""
    global banned_users, authorized_users, message_log, user_profiles, conversations, user_context
    banned_users = set(state.get('banned_users', []))
    authorized_users = set(state.get('authorized_users', []))
    message_log = state.get('message_log', [])
    for k, v in state.get('user_profiles', {}).items():
        user_profiles[int(k)] = v
    for k, v in state.get('conversations', {}).items():
        conversations[int(k)] = v
    for k, v in state.get('user_context', {}).items():
        user_context[int(k)] = v
    _expire_context()

def trim_convs():
    """Trim conversations to last 5 exchanges per chat."""
    for cid in list(conversations.keys()):
        conversations[cid] = conversations[cid][-10:]

def _expire_context():
    now = time.time()
    stale = [u for u, c in user_context.items() if now - c.get('timestamp', 0) > 1800]
    for u in stale:
        del user_context[u]

def _build_state():
    trim_convs()
    _expire_context()
    return {
        "banned_users": list(banned_users),
        "authorized_users": list(authorized_users),
        "message_log": message_log[-1000:],
        "user_profiles": {str(k): v for k, v in user_profiles.items()},
        "conversations": {str(k): v for k, v in conversations.items()},
        "user_context": {str(k): v for k, v in user_context.items()},
        "updated_at": int(time.time())
    }

def _save_state():
    """Persist state via whichever backend is configured (Upstash > SQLite > Telegram pin)."""
    global state_msg_id
    state = _build_state()
    if UPSTASH_URL and UPSTASH_TOKEN:
        text = json.dumps(state, separators=(',', ':'))
        if _upstash_cmd("SET", "saom:state", text) is not None:
            return
        log.warning("Upstash save failed, falling through to next backend")
    if MEMORY_DB_PATH:
        try:
            conn = _sqlite_conn()
            conn.execute("INSERT INTO kv (key, value) VALUES ('saom:state', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (json.dumps(state, separators=(',', ':')),))
            conn.commit()
            conn.close()
            return
        except Exception as e:
            log.warning(f"SQLite save failed, falling through to next backend: {e}")
    if not STORAGE_CHAT_ID:
        return
    text = json.dumps(state, separators=(',', ':'))
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/"
    try:
        if state_msg_id:
            req = Request(api + "editMessageText",
                data=json.dumps({'chat_id': int(STORAGE_CHAT_ID), 'message_id': state_msg_id, 'text': text}).encode(),
                headers={'Content-Type': 'application/json'}, method='POST')
            urlopen(req, timeout=10)
        else:
            req = Request(api + "sendMessage",
                data=json.dumps({'chat_id': int(STORAGE_CHAT_ID), 'text': text}).encode(),
                headers={'Content-Type': 'application/json'}, method='POST')
            resp = json.loads(urlopen(req, timeout=10).read())
            if resp.get('ok'):
                state_msg_id = resp['result']['message_id']
                req = Request(api + "pinChatMessage",
                    data=json.dumps({'chat_id': int(STORAGE_CHAT_ID), 'message_id': state_msg_id}).encode(),
                    headers={'Content-Type': 'application/json'}, method='POST')
                urlopen(req, timeout=10)
    except Exception as e:
        log.warning(f"State save failed (all backends exhausted): {e}")

def _restore_state():
    """Load state on startup via whichever backend is configured (Upstash > SQLite > Telegram pin)."""
    global state_msg_id
    if UPSTASH_URL and UPSTASH_TOKEN:
        text = _upstash_cmd("GET", "saom:state")
        if text:
            try:
                _apply_state(json.loads(text))
                log.info(f"State restored from Upstash: {len(message_log)} msgs, {len(user_profiles)} users")
                return
            except Exception as e:
                log.warning(f"Upstash state parse failed: {e}")
    if MEMORY_DB_PATH:
        try:
            conn = _sqlite_conn()
            row = conn.execute("SELECT value FROM kv WHERE key='saom:state'").fetchone()
            conn.close()
            if row:
                _apply_state(json.loads(row[0]))
                log.info(f"State restored from SQLite: {len(message_log)} msgs, {len(user_profiles)} users")
                return
        except Exception as e:
            log.warning(f"SQLite state restore failed: {e}")
    if not STORAGE_CHAT_ID:
        return
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/"
    try:
        req = Request(api + "getChat",
            data=json.dumps({'chat_id': int(STORAGE_CHAT_ID)}).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        resp = json.loads(urlopen(req, timeout=10).read())
        if not resp.get('ok'):
            return
        pinned = resp['result'].get('pinned_message')
        if not pinned:
            return
        state_msg_id = pinned['message_id']
        text = pinned.get('text', pinned.get('caption', ''))
        if not text:
            return
        _apply_state(json.loads(text))
        log.info(f"State restored from Telegram pin: {len(message_log)} msgs, {len(banned_users)} bans, {len(user_profiles)} users, {len(conversations)} convs")
        _save_state()  # update timestamp
    except Exception as e:
        log.warning(f"State restore failed: {e}")

def _load_auth():
    """Merge in the local-file authorized list (fallback for non-Render/no-STORAGE_CHAT_ID
    deployments) on top of whatever _restore_state() already loaded from the pinned
    Telegram message, which is the primary source of truth on Render."""
    global authorized_users
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE) as f:
                data = json.load(f)
                authorized_users |= set(data.get("authorized", []))
        except Exception:
            pass
    if OM_CHAT_ID and OM_CHAT_ID != '0':
        owner_id = int(OM_CHAT_ID)
        authorized_users.add(owner_id)
    _save_auth()
    _save_state()

def _save_auth():
    data = {"authorized": sorted(list(authorized_users))}
    with open(AUTH_FILE, 'w') as f:
        json.dump(data, f, indent=2)

# ── Conversation memory ──
MAX_HISTORY = 10
conversations = {}  # chat_id -> list of {"role": str, "content": str}
user_profiles = {}  # chat_id -> {name, username, first_seen, msg_count, lang}
message_log = []  # list of {chat_id, name, username, text, time} — master log
banned_users = set()  # chat_ids that are banned
user_context = {}  # user_id -> {last_topic, last_msg, last_reply, timestamp}

OM_CHAT_ID = os.environ.get('OM_CHAT_ID', '0')
authorized_users = set()  # chat_ids authorized to use the bot
save_counter = 0  # save state every N messages
START_TIME = time.time()
last_request_time = {}  # chat_id -> timestamp, for basic rate limiting
RATE_LIMIT_SECONDS = 2  # min gap between LLM requests from the same chat
unauth_notice_sent = {}  # chat_id -> timestamp, so we don't spam the "not authorized" notice
UNAUTH_NOTICE_COOLDOWN = 600  # only re-send the notice once per 10 minutes per chat

def _load(p):
    try:
        with open(os.path.join(BASE, p)) as f: return json.load(f)
    except: return {}

def clean_name(raw):
    """Strip invisible/control chars from names."""
    import unicodedata
    cleaned = []
    for ch in raw:
        cat = unicodedata.category(ch)
        if cat == 'Cf' or (cat.startswith('C') and cat != 'Cs'):
            continue
        cleaned.append(ch)
    return ''.join(cleaned).strip() or 'User'

def get_profile(chat_id, msg):
    """Create or update user profile from message."""
    raw_name = msg.get('from', {}).get('first_name', 'Unknown')
    if chat_id not in user_profiles:
        user_profiles[chat_id] = {
            "name": clean_name(raw_name),
            "username": msg.get('from', {}).get('username', ''),
            "first_seen": int(time.time()),
            "msg_count": 0,
            "lang": msg.get('from', {}).get('language_code', 'en'),
            "chat_id": chat_id
        }
    p = user_profiles[chat_id]
    p["name"] = clean_name(raw_name)
    p["msg_count"] += 1
    p["last_seen"] = int(time.time())
    return p

def _api_retry(req, max_retries=4):
    """POST with exponential backoff on 429."""
    delay = 1
    for attempt in range(max_retries):
        try:
            return json.loads(urlopen(req, timeout=60).read())
        except Exception as e:
            err_str = str(e)
            if '429' in err_str and attempt < max_retries - 1:
                log.warning(f"429 rate limit, retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2
            else:
                raise
    raise Exception("Max retries exceeded")

def _rate_limited(chat_id):
    """Basic per-chat cooldown to stop rapid-fire spam from burning through the
    Groq quota / bill. Returns True (and records the hit) if this chat must wait."""
    now = time.time()
    last = last_request_time.get(chat_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    last_request_time[chat_id] = now
    return False

# ── LLM with history ──
def ask_llm(chat_id, user_msg):
    if _rate_limited(chat_id):
        return "Whoa, slow down a bit — try again in a couple seconds."
    if chat_id not in conversations:
        conversations[chat_id] = []
    history = conversations[chat_id][-(MAX_HISTORY*2):]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in history:
        messages.append(h)
    # Non-math detection — expanded to catch GK, physics, general questions
    non_math_words = ['who is', 'what is', 'what are', 'where is', 'when did', 'why does',
                      'how does', 'how do', 'which is', 'who created', 'define', 'explain',
                      'tell me about', 'difference between', 'what was', 'what does',
                      'who are', 'what did', 'where do', 'when was',
                      'newton', 'force', 'gravity', 'acceleration', 'velocity',
                      'national', 'capital of', 'president', 'prime minister',
                      'born', 'invent', 'discover', 'invented', 'discovered']
    is_non_math = any(kw in user_msg.lower() for kw in non_math_words)
    # Math detection — only flag =? if accompanied by math-domain content
    has_eq_question = '=?' in user_msg.lower() or '= ?' in user_msg.lower()
    has_math_domain = bool(re.search(r'\d+\s*[×÷+\-]\s*\d+|^\d+\s*=|[=×÷]\s*\d+\s*[=×÷]| \d+x | \d+y ', user_msg.lower())) or \
                      any(kw in user_msg.lower() for kw in ['km/h', 'm/s', 'ratio', 'profit', 'loss', 'interest',
                            'speed', 'time', 'work', 'age', 'avg', 'area', 'volume', 'perimeter',
                            'train', 'boat', 'stream', 'mixture', 'allegation', 'sum', 'divide'])
    # Detect SOLUTION pattern: user is posting equations as an answer, not asking a question
    solution_patterns = [
        r'^\s*(?:\w+\s*[:=]\s*)?\-?\d+\s*$',     # standalone "x = 65" or "y = 53"
        r'^\s*\w+\s*\+?\s*\w+\s*=\s*\d+',         # "x + y = 118"
        r'^\s*\w+\s*=\s*\d+\s*[-+×÷]\s*\d+',     # "x = 855 - 530"
        r'^\d+\s*[-+]\s*\d+\s*=\s*\d+',            # "855 - 590 = 265"
    ]
    is_solution_share = any(re.match(p, user_msg.strip()) for p in solution_patterns) and \
                        not any(kw in user_msg.lower() for kw in ['solve', 'find', 'calculate', 'what', 'how'])
    math_priority = ['calculate', 'solve', 'find']
    math_keywords = ['math', 'ratio', 'profit', 'loss', 'interest', 'speed', 'time', 'work', 'age', 'avg',
                     'area', 'volume', 'perimeter', 'train', 'boat', 'stream', 'mixture', 'allegation',
                     'number', 'digit', 'sum', 'difference', 'product', 'divide', 'multiple',
                     '%', '÷', '×', '=x', '=y']
    # Determine if math: priority words, or math domain keywords (not non-math), or =? with math domain content
    is_math = any(kw in user_msg.lower() for kw in math_priority) or \
              (any(kw in user_msg.lower() for kw in math_keywords) and not is_non_math) or \
              (has_eq_question and has_math_domain and not is_non_math)
    if not is_math and not is_non_math:
        is_math = bool(re.search(r'\d+\s*:\s*\d+', user_msg)) and not has_eq_question
    # If user is posting a solution, treat as non-math response (acknowledge, don't re-solve)
    if is_solution_share and not any(kw in user_msg.lower() for kw in ['solve', 'find', 'calculate', '?', 'how to']):
        is_math = False
    if is_math:
        concise_msg = "Solve this carefully, then output your final answer as ONLY 3-7 equation lines. One equation per line. Intermediate working shown. Final answer on last line. No LaTeX. No prose commentary in the final output. JUST EQUATIONS.\n\n" + user_msg
        temp = 0.2
        maxtok = 2048
        reasoning_effort = "high"
    elif is_solution_share:
        concise_msg = "The user posted an answer/solution. Confirm it briefly and supportively. If correct, say 'Correct'. If wrong, give the right answer in 2-3 lines. No equations format.\n\n" + user_msg
        temp = 0.5
        maxtok = 256
        reasoning_effort = "low"
    else:
        concise_msg = "Answer naturally and helpfully. No LaTeX. DO NOT use equation format for non-math questions.\n\n" + user_msg
        temp = 0.7
        maxtok = 1024
        reasoning_effort = "low"
    messages.append({"role": "user", "content": concise_msg})
    body_dict = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": maxtok, "temperature": temp
    }
    if MODEL.startswith("openai/gpt-oss") or MODEL.startswith("qwen/qwen3"):
        body_dict["reasoning_effort"] = reasoning_effort
        body_dict["reasoning_format"] = "hidden"
    body = json.dumps(body_dict).encode()
    req = Request("https://api.groq.com/openai/v1/chat/completions",
                  data=body,
                  headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json",
                           "User-Agent": "Mozilla/5.0 (compatible; SAOM-bot/1.0)"},
                  method="POST")
    try:
        resp = _api_retry(req)['choices'][0]['message']['content'].strip()
        resp = strip_think(resp)
        resp = strip_latex(resp)
        conversations[chat_id].append({"role": "user", "content": user_msg})
        conversations[chat_id].append({"role": "assistant", "content": resp})
        return resp
    except Exception as e:
        return f"LLM error: {e}"

def ask_llm_vision(chat_id, prompt, image_data, caption=""):
    """Send prompt + image to Groq vision model."""
    if _rate_limited(chat_id):
        return "Whoa, slow down a bit — try again in a couple seconds."
    import base64
    b64 = base64.b64encode(image_data).decode()
    if image_data[:4] == b'\x89PNG':
        mime = "image/png"
    elif image_data[:2] == b'\xff\xd8':
        mime = "image/jpeg"
    elif image_data[:4] == b'RIFF':
        mime = "image/webp"
    else:
        mime = "image/jpeg"
    # Math detection for vision too
    non_math_words = ['who', 'what is', 'what are', 'where', 'when', 'why', 'how', 'which',
                      'who created', 'define', 'explain', 'tell me about', 'difference between',
                      'who are', 'what did', 'where do', 'when was',
                      'newton', 'force', 'gravity', 'acceleration', 'velocity',
                      'national', 'capital of', 'president', 'prime minister',
                      'born', 'invent', 'discover', 'invented', 'discovered']
    is_non_math = any(kw in prompt.lower() for kw in non_math_words)
    math_keywords = ['math', 'solve', 'find', 'calculate', 'km', 'ratio', 'profit', 'loss',
                     'speed', 'time', 'work', 'area', 'volume', 'perimeter', 'sum', 'divide',
                     '%', '÷', '×', '=x', '=y']
    has_eq_question = '=?' in prompt.lower() or '= ?' in prompt.lower()
    has_math_domain = bool(re.search(r'\d+\s*[×÷+\-]\s*\d+|^\d+\s*=', prompt.lower()))
    is_math = any(kw in prompt.lower() for kw in math_keywords) and not is_non_math
    if has_eq_question and has_math_domain and not is_non_math:
        is_math = True
    if not is_math and not is_non_math:
        is_math = bool(re.search(r'\d+\s*:\s*\d+', prompt)) and not has_eq_question
    if is_math:
        vision_prompt = "Read the problem carefully and solve it step by step internally, then output your final answer as ONLY 3-7 equation lines. One equation per line. Intermediate working shown. Final answer on last line. No LaTeX. No prose commentary in the final output. JUST EQUATIONS.\n\n" + prompt
        vtemp = 0.2
        vmaxtok = 2048
        v_reasoning_effort = "high"
    else:
        vision_prompt = "Answer naturally and helpfully. No LaTeX.\n\n" + prompt
        vtemp = 0.3
        vmaxtok = 4096
        v_reasoning_effort = "low"
    content = []
    if caption:
        content.append({"type": "text", "text": f"Caption: {caption}"})
    content.append({"type": "text", "text": vision_prompt})
    content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    vbody_dict = {
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": vmaxtok, "temperature": vtemp
    }
    if VISION_MODEL.startswith("qwen/qwen3") or VISION_MODEL.startswith("openai/gpt-oss"):
        vbody_dict["reasoning_effort"] = v_reasoning_effort
        vbody_dict["reasoning_format"] = "hidden"
    body = json.dumps(vbody_dict).encode()
    log.info(f"Vision request: model={VISION_MODEL}, caption={'yes' if caption else 'no'}, img_size={len(image_data)} bytes")
    req = Request("https://api.groq.com/openai/v1/chat/completions",
                  data=body,
                  headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json",
                           "User-Agent": "Mozilla/5.0 (compatible; SAOM-bot/1.0)"},
                  method="POST")
    try:
        resp = _api_retry(req)['choices'][0]['message']['content'].strip()
        resp = strip_think(resp)
        resp = strip_latex(resp)
        if chat_id in conversations:
            conversations[chat_id].append({"role": "user", "content": f"[Image analysis] {prompt[:100]}"})
            conversations[chat_id].append({"role": "assistant", "content": resp})
        return resp
    except Exception as e:
        return f"Vision LLM error: {e}"

# ── SAOM tools ──
TELEGRAM_MAX_LEN = 4096

def send_message(api, chat_id, text, reply_to_message_id=None):
    """Send a message, truncating to Telegram's length limit and falling back
    to plain text if Markdown parsing fails (malformed markdown would otherwise
    cause Telegram to reject the message and drop the reply entirely)."""
    if len(text) > TELEGRAM_MAX_LEN:
        text = text[:TELEGRAM_MAX_LEN - 20].rstrip() + "\n… [truncated]"
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}
    if reply_to_message_id:
        payload['reply_to_message_id'] = reply_to_message_id
    try:
        req = Request(f"{api}/sendMessage", data=json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json'}, method='POST')
        resp = json.loads(urlopen(req, timeout=10).read())
        if resp.get('ok'):
            return
    except Exception as e:
        log.warning(f"sendMessage (markdown) failed: {e}")
    # Fallback: retry without markdown parsing so the user still gets a reply
    try:
        payload.pop('parse_mode', None)
        req = Request(f"{api}/sendMessage", data=json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json'}, method='POST')
        urlopen(req, timeout=10)
    except Exception as e:
        log.error(f"sendMessage (plain) failed: {e}")

def _is_safe_url(url):
    """Block requests to loopback, private, and link-local addresses (incl. cloud
    metadata endpoints like 169.254.169.254) to stop /fetch and /webhook from being
    used as an SSRF pivot into the hosting network. Only plain http(s) is allowed."""
    import socket
    import ipaddress
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return False
        for info in socket.getaddrinfo(parsed.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False

def send_document(chat_id, file_bytes, filename):
    """Send a file to Telegram chat."""
    boundary = '----boundary' + str(int(time.time()))
    body = []
    body.append(f'--{boundary}')
    body.append(f'Content-Disposition: form-data; name="chat_id"')
    body.append('')
    body.append(str(chat_id))
    body.append(f'--{boundary}')
    body.append(f'Content-Disposition: form-data; name="document"; filename="{filename}"')
    body.append('Content-Type: text/plain')
    body.append('')
    body.append(file_bytes.decode() if isinstance(file_bytes, bytes) else file_bytes)
    body.append(f'--{boundary}--')
    payload = '\r\n'.join(body).encode()
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    req = Request(api, data=payload,
                  headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
                  method='POST')
    urlopen(req, timeout=15)

def agent_process(chat_id, prompt):
    prompt = prompt.strip()
    if not prompt: return "Say something!"
    pl = prompt.lower()

    if pl in ('health', '/health', '/h'):
        import socket as _socket
        host = _socket.gethostname()
        return f"SAOM is healthy and running on Render! (build {BUILD_TAG}, model={MODEL}, host={host}, pid={os.getpid()})"
    if pl in ('ping', '/ping'):
        return "Pong!"
    if pl in ('uptime', '/uptime'):
        secs = int(time.time() - START_TIME)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"Uptime: {h}h {m}m {s}s"
    if pl in ('stats', '/stats', '/st'):
        init = _load('init.json')
        ms = init.get('memory_stats', {})
        skills = init.get('loaded_skills', [])
        return (f"SAOM v{init.get('version','?')} | {init.get('session_count',0)} sessions | "
                f"{ms.get('graph_nodes',0)}n/{ms.get('graph_edges',0)}e | "
                f"{len(skills)} skills | {init.get('tools_count',0)} tools")
    if pl in ('version', '/version', '/v'):
        return f"SAOM v{_load('init.json').get('version','?')}"
    if pl in ('help', '/help', '/start'):
        return ("I am **SAOM** (Super Agent Ouroboros Manager), created by **Om**.\n"
                "I'm a recursive self-improving AI agent with:\n"
                "- Conversation memory (I remember our chat)\n"
                "- Full Groq LLM responses\n"
                "- User profiling (I know who you are)\n\n"
                "**Commands:**\n"
                "- /health, /ping, /uptime, /stats, /version\n"
                "- /save — download chat as .txt file\n"
                "- /logs — show recent messages\n"
                "- /clear — wipe conversation\n"
                "- /whoami — see what I know about you\n"
                "- /fetch <url> — fetch a webpage\n"
                "- /webhook <url> <json> — call external API\n"
                "- /userlog — master user log (Om only)\n"
                "- /userlogcsv — export CSV (Om only)\n"
                "Try asking me anything!")
    if pl in ('save', '/save', '/s'):
        if chat_id not in conversations or len(conversations[chat_id]) < 2:
            return "No conversation to save yet."
        lines = [f"SAOM Chat Log -- {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}"]
        lines.append("=" * 50)
        for msg in conversations[chat_id]:
            role = "You" if msg['role'] == 'user' else "SAOM"
            lines.append(f"\n[{role}]: {msg['content']}")
        content = '\n'.join(lines)
        send_document(chat_id, content, f"saom-chat-{int(time.time())}.txt")
        return "Conversation saved! Check the file above."
    if pl in ('logs', '/logs', '/l'):
        if chat_id not in conversations or len(conversations[chat_id]) < 2:
            return "No conversation history."
        history = conversations[chat_id]
        last_n = min(6, len(history))
        lines = [f"[{h['role']}]: {h['content'][:100]}" for h in history[-last_n:]]
        return "Recent messages:\n" + '\n'.join(lines) + "\n\nUse /save to download full chat."
    if pl in ('clear', '/clear', '/c'):
        conversations[chat_id] = []
        return "Conversation cleared!"
    if pl in ('whoami', '/whoami', '/who'):
        p = user_profiles.get(chat_id, {})
        return (f"**Your Profile**\n"
                f"- Name: {p.get('name', '?')}\n"
                f"- Username: @{p.get('username', 'none')}\n"
                f"- Messages sent: {p.get('msg_count', 0)}\n"
                f"- Language: {p.get('lang', 'en')}\n"
                f"- Chat ID: `{chat_id}`\n"
                f"- First seen: <code>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(p.get('first_seen', 0)))}</code>")

    # Ban/unban - Om only
    if (prompt.startswith('ban ') or prompt.startswith('/ban ')) and str(chat_id) == OM_CHAT_ID:
        parts = prompt.split()
        if len(parts) < 2:
            return "Usage: /ban <chat_id>"
        try:
            target = int(parts[1])
            banned_users.add(target)
            _save_state()
            return f"Banned `{target}`. They can no longer use the bot."
        except ValueError:
            return "Invalid chat_id. Must be a number."

    if (prompt.startswith('unban ') or prompt.startswith('/unban ')) and str(chat_id) == OM_CHAT_ID:
        parts = prompt.split()
        if len(parts) < 2:
            return "Usage: /unban <chat_id>"
        try:
            target = int(parts[1])
            banned_users.discard(target)
            _save_state()
            return f"Unbanned `{target}`. They can use the bot again."
        except ValueError:
            return "Invalid chat_id. Must be a number."

    if pl in ('banlist', '/banlist', '/bans') and str(chat_id) == OM_CHAT_ID:
        if not banned_users:
            return "No users are banned."
        lines = ["**Banned Users**"]
        for cid in sorted(banned_users):
            name = user_profiles.get(cid, {}).get('name', 'Unknown')
            lines.append(f"- `{cid}` ({name})")
        return '\n'.join(lines)

    # Authorized user management - Om only
    if prompt.startswith('auth ') or prompt.startswith('/auth '):
        if str(chat_id) != OM_CHAT_ID:
            return "Only the bot owner can manage authorized users."
        parts = prompt.split()
        if len(parts) < 2:
            return "Usage: /auth <add|remove|list> [chat_id]"
        cmd = parts[1].lower()
        if cmd == 'list':
            if not authorized_users:
                return "No authorized users. Add one with `/auth add <chat_id>`."
            lines = [f"**Authorized Users** ({len(authorized_users)})"]
            for cid in sorted(authorized_users):
                name = user_profiles.get(cid, {}).get('name', 'Unknown')
                marker = " (owner)" if str(cid) == OM_CHAT_ID else ""
                lines.append(f"- `{cid}` ({name}){marker}")
            return '\n'.join(lines)
        if len(parts) < 3:
            return "Usage: /auth <add|remove> <chat_id>"
        try:
            target = int(parts[2])
        except ValueError:
            return "Invalid chat_id. Must be a number."
        if cmd == 'add':
            authorized_users.add(target)
            _save_auth()
            _save_state()
            return f"Added `{target}` to authorized users."
        elif cmd == 'remove':
            if str(target) == OM_CHAT_ID:
                return "Cannot remove the owner from authorized users."
            authorized_users.discard(target)
            _save_auth()
            _save_state()
            return f"Removed `{target}` from authorized users."
        else:
            return "Usage: /auth <add|remove|list> [chat_id]"

    # Webhook / talk to other bots
    if prompt.startswith('webhook ') or prompt.startswith('/webhook ') or prompt.startswith('wh ') or prompt.startswith('/wh '):
        parts = prompt.split(maxsplit=2)
        if len(parts) < 3:
            return "Usage: /webhook <url> <json_body>"
        url, body_str = parts[1], parts[2]
        if not _is_safe_url(url):
            return "Refusing to call that URL (blocked: local/private/internal address)."
        try:
            body = json.loads(body_str)
            data = json.dumps(body).encode()
            req = Request(url, data=data,
                          headers={'Content-Type': 'application/json', 'User-Agent': 'SAOM-bot/1.0'},
                          method='POST')
            resp = json.loads(urlopen(req, timeout=15).read())
            return f"Webhook response:\n{json.dumps(resp, indent=2)[:1500]}"
        except Exception as e:
            return f"Webhook error: {e}"

    # ── Telegram link reader ──
    tg_match = re.search(r'(?:https?://)?(?:t\.me|telegram\.me)/(?:c/)?([a-zA-Z0-9_]+)(?:/(\d+))?', prompt)
    if tg_match:
        channel = tg_match.group(1)
        msg_id = tg_match.group(2)
        if msg_id:
            try:
                url = f"https://t.me/{channel}/{msg_id}?embed=1"
                req = Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                resp = urlopen(req, timeout=15).read().decode('utf-8', errors='replace')
                text_match = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*</div>', resp, re.DOTALL)
                author_match = re.search(r'<div class="tgme_widget_message_author[^"]*"[^>]*>(.*?)</div>', resp, re.DOTALL)
                author = re.sub(r'<[^>]+>', '', author_match.group(1)).strip() if author_match else f"@{channel}"
                text = ""
                if text_match:
                    raw = text_match.group(1)
                    raw = raw.replace('<br/>', '\n').replace('<br>', '\n').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&#39;', "'")
                    text = re.sub(r'<[^>]+>', '', raw).strip()
                content_parts = [f"From {author}"]
                if text:
                    content_parts.append(text)
                # Check for media (photo / document / video)
                photo_urls = re.findall(r'background-image:url\([ "\']+(https?://[^"\' )]+)[ "\']+\)', resp)
                doc_urls = re.findall(r'class="tgme_widget_message_download"[^>]*href="(https?://[^"]+)"', resp)
                video_urls = re.findall(r'<video[^>]*src="(https?://[^"]+)"', resp)
                if photo_urls:
                    img_url = photo_urls[0].replace('&amp;', '&')
                    try:
                        img_req = Request(img_url, headers={'User-Agent': 'Mozilla/5.0'})
                        img_data = urlopen(img_req, timeout=20).read()
                        return ask_llm_vision(chat_id, f"Read all text in this image. If the image has Hindi/Devanagari text, read and preserve it exactly. Format math with Unicode (×, ÷, ≠, √, ², ³, ½, →, ∠, △, ⟂). Separate lines for equations. NEVER use LaTeX (no backslash-parentheses, no dollar signs, no backslash commands). User's request: {prompt}", img_data, caption=text)
                    except Exception as e:
                        return f"Could not download image from Telegram: {e}"
                if doc_urls:
                    doc_url = doc_urls[0].replace('&amp;', '&')
                    doc_name_match = re.search(r'<div class="tgme_widget_message_document_title">([^<]+)</div>', resp)
                    doc_name = doc_name_match.group(1).strip() if doc_name_match else "document"
                    content_parts.append(f"[File: {doc_name}]")
                    content_parts.append(f"[Download: {doc_url}]")
                if video_urls:
                    content_parts.append("[This message contains a video - cannot process]")
                if not text and not photo_urls and not doc_urls and not video_urls:
                    return f"Could not extract any content from @{channel} #{msg_id}. The channel may be private or the message may contain unsupported media."
                combined = '\n\n'.join(content_parts)
                return ask_llm(chat_id, f"Here is a Telegram message content:\n\n{combined}\n\nUser asked: {prompt}")
            except Exception as e:
                return f"Error fetching Telegram message: {e}"
        else:
            return f"Telegram channel/profile: @{channel}. Use a specific message link like `t.me/{channel}/<message_id>` to read a message."
    if prompt.startswith('get ') or prompt.startswith('/get ') or prompt.startswith('fetch ') or prompt.startswith('/fetch '):
        url = prompt.split(maxsplit=1)[1]
        if not _is_safe_url(url):
            return "Refusing to fetch that URL (blocked: local/private/internal address)."
        try:
            req = Request(url, headers={'User-Agent': 'SAOM-bot/1.0'})
            resp = urlopen(req, timeout=15).read().decode('utf-8', errors='replace')
            return f"Response ({len(resp)} chars):\n{resp[:1500]}"
        except Exception as e:
            return f"Fetch error: {e}"

    # Master user log - restricted to Om only
    if pl in ('userlog', '/userlog', 'ul') and str(chat_id) == OM_CHAT_ID:
        if not message_log:
            return "No messages logged yet."
        users = {}
        for entry in message_log:
            cid = entry['chat_id']
            if cid not in users:
                users[cid] = {"name": entry['name'], "username": entry['username'], "count": 0, "first": entry['time'], "last": entry['time']}
            users[cid]["count"] += 1
            users[cid]["last"] = max(users[cid]["last"], entry['time'])
        lines = ["**Master User Log**", f"Total messages: {len(message_log)}", f"Unique users: {len(users)}", ""]
        for cid, u in sorted(users.items(), key=lambda x: -x[1]['count']):
            first = time.strftime('%m-%d %H:%M', time.localtime(u['first']))
            last = time.strftime('%m-%d %H:%M', time.localtime(u['last']))
            lines.append(f"`{cid}` | {u['name']} @{u['username']} | {u['count']} msgs | {first} -> {last}")
        return '\n'.join(lines)

    if (prompt.startswith('userlogcsv') or prompt.startswith('/userlogcsv')) and str(chat_id) == OM_CHAT_ID:
        if not message_log:
            return "No messages logged yet."
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['chat_id', 'name', 'username', 'text', 'unix_time', 'human_time'])
        for e in message_log:
            w.writerow([e['chat_id'], e['name'], e['username'], e['text'], e['time'],
                       time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e['time']))])
        content = buf.getvalue()
        buf.close()
        send_document(chat_id, content, f"saom-userlog-{int(time.time())}.csv")
        return f"CSV exported ({len(message_log)} messages). Check the file above."

    # Shell exec - restricted to Om only
    if (prompt.startswith('exec ') or prompt.startswith('/exec ') or prompt.startswith('run ') or prompt.startswith('/run ')) and str(chat_id) == OM_CHAT_ID:
        cmd = prompt.split(maxsplit=1)[1]
        import subprocess
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            out = (r.stdout or '') + (r.stderr or '')
            return f"`$ {cmd}`\n```\n{out[:2000]}```"
        except Exception as e:
            return f"Exec error: {e}"

    # Catch Om-only commands that fell through (wrong chat_id or OM_CHAT_ID not set)
    om_only = ['userlog', 'userlogcsv', 'ul', '/userlog', '/userlogcsv']
    if any(pl.startswith(c) or pl == c for c in om_only):
        return "This command is restricted to the bot owner (OM_CHAT_ID). Set OM_CHAT_ID env var."
    if pl.startswith('exec ') or pl.startswith('/exec ') or pl.startswith('run ') or pl.startswith('/run '):
        return "This command is restricted to the bot owner (OM_CHAT_ID). Set OM_CHAT_ID env var."

    return ask_llm(chat_id, prompt)

# ── Telegram polling ──
admin_cache = {}  # chat_id -> (timestamp, set_of_admin_user_ids)
BOT_USERNAME = None

def _is_admin(api, chat_id, user_id):
    """Check if user is admin in a group. Cached for 5 min."""
    if chat_id not in admin_cache or time.time() - admin_cache[chat_id][0] > 300:
        try:
            r = json.loads(urlopen(f"{api}/getChatAdministrators?chat_id={chat_id}", timeout=10).read())
            admin_cache[chat_id] = (time.time(), {a['user']['id'] for a in r.get('result', [])})
        except:
            return False
    return user_id in admin_cache[chat_id][1]

def poll():
    global BOT_USERNAME
    api = f"https://api.telegram.org/bot{BOT_TOKEN}"
    offset = 0
    try:
        me = json.loads(urlopen(f"{api}/getMe", timeout=10).read())
        BOT_USERNAME = me['result']['username'].lower()
        log.info(f"Bot username: @{BOT_USERNAME}")
    except:
        BOT_USERNAME = ""
    try:
        wh_info = json.loads(urlopen(f"{api}/getWebhookInfo", timeout=10).read())
        if wh_info.get('result', {}).get('url'):
            log.warning(f"A webhook was registered ({wh_info['result']['url']}) — this "
                        f"blocks getUpdates from receiving anything. Removing it now.")
            urlopen(Request(f"{api}/deleteWebhook", data=b'{}',
                    headers={'Content-Type': 'application/json'}, method='POST'), timeout=10)
    except Exception as e:
        log.warning(f"Could not check/clear webhook: {e}")
    log.info("Bot polling started")
    while True:
        try:
            r = json.loads(urlopen(f"{api}/getUpdates?offset={offset}&timeout=30", timeout=35).read())
            for upd in r.get('result', []):
                offset = upd['update_id'] + 1
                try:
                    msg = upd.get('message', {})
                    text = msg.get('text', '').strip()
                    chat_id = msg.get('chat', {}).get('id')
                    if not chat_id: continue
                    chat_type = msg.get('chat', {}).get('type', 'private')
                    user_id = msg.get('from', {}).get('id')
                    caption = msg.get('caption', '').strip()
                    # In groups: only respond if bot is @mentioned (check both text and caption)
                    if chat_type in ('group', 'supergroup'):
                        check_text = (text + " " + caption).lower()
                        if f"@{BOT_USERNAME}" not in check_text:
                            continue
                        # Strip mention for cleaner prompt (case-insensitive)
                        text = re.sub(rf'@{re.escape(BOT_USERNAME)}\b', '', text, flags=re.I).strip()
                        caption = re.sub(rf'@{re.escape(BOT_USERNAME)}\b', '', caption, flags=re.I).strip()
                        # Only admins can use the bot in groups
                        if not _is_admin(api, chat_id, user_id):
                            continue
                    if chat_id in banned_users and str(chat_id) != OM_CHAT_ID:
                        continue
                    if chat_id not in authorized_users and str(chat_id) != OM_CHAT_ID:
                        # Log them (so the owner can find their real chat_id via /userlog)
                        # and tell them their ID once, instead of silent, untraceable drop.
                        get_profile(chat_id, msg)
                        message_log.append({
                            "chat_id": chat_id,
                            "name": clean_name(msg.get('from', {}).get('first_name', '?')),
                            "username": msg.get('from', {}).get('username', ''),
                            "text": (text or caption or '[non-text message]')[:300],
                            "time": int(time.time()),
                            "role": "user"
                        })
                        if chat_type == 'private' and time.time() - unauth_notice_sent.get(chat_id, 0) > UNAUTH_NOTICE_COOLDOWN:
                            unauth_notice_sent[chat_id] = time.time()
                            send_message(api, chat_id,
                                f"You're not authorized to use this bot yet.\n"
                                f"Your chat ID is `{chat_id}` — this is a Telegram ID, "
                                f"*not* your phone number. Ask the bot owner to run "
                                f"`/auth add {chat_id}`.")
                        continue
                    get_profile(chat_id, msg)
                    msg_id = msg.get('message_id')
                    photo = msg.get('photo')
                    is_photo = bool(photo)
                    if is_photo:
                        prompt = caption or "Describe this image"
                    elif not text:
                        continue
                    else:
                        prompt = text[1:] if text.startswith('/') else text
                    # Auto-read replied-to message for context
                    uid = msg.get('from', {}).get('id')
                    is_solve = text.lower().startswith('/solve') or text.lower().startswith('solve ')
                    reply_to = msg.get('reply_to_message')
                    if reply_to:
                        rtext = reply_to.get('text', '').strip()
                        rcaption = reply_to.get('caption', '').strip()
                        rphoto = reply_to.get('photo')
                        if rphoto:
                            is_photo = True
                            photo = rphoto
                            caption = rcaption
                            if is_solve:
                                prompt = "Solve this problem from the image. SHORT ANSWER ONLY."
                        elif is_solve:
                            prompt = f"Solve this. SHORT ANSWER ONLY (just result): {rtext[:500]}"
                        elif rtext:
                            prompt = f"Replied to: {rtext[:500]}\nUser: {prompt}"
                    elif is_solve:
                        query = prompt[6:].strip() if len(text) > 6 else ""
                        prompt = f"SHORT ANSWER ONLY (just result): {query}" if query else "SHORT ANSWER ONLY."
                    # Inject user context for follow-ups (no reply, no solve)
                    if uid and uid in user_context and not reply_to and not is_solve and not prompt.startswith('/'):
                        ctx = user_context[uid]
                        if time.time() - ctx.get('timestamp', 0) < 1800:
                            prompt = f"[Context: {ctx.get('last_topic', '')[:100]}]\n{prompt}"
                    # Show typing indicator while processing
                    urlopen(Request(f"{api}/sendChatAction", 
                        data=json.dumps({'chat_id': chat_id, 'action': 'typing'}).encode(),
                        headers={'Content-Type': 'application/json'}, method='POST'), timeout=5)
                    if is_photo:
                        file_id = photo[-1]['file_id']
                        try:
                            fr = json.loads(urlopen(f"{api}/getFile?file_id={file_id}", timeout=10).read())
                            fpath = fr['result']['file_path']
                            img_data = urlopen(f"{api.replace('/bot', '/file/bot')}/{fpath}", timeout=20).read()
                            # Check for /solve in caption or text before running vision
                            check_solve = (caption + " " + text).lower().strip()
                            has_solve = check_solve.startswith('/solve') or check_solve.startswith('solve ')
                            if has_solve:
                                resp = ask_llm_vision(chat_id, f"Read all text in this image. If the image has Hindi/Devanagari text, read and preserve it exactly. Format math with Unicode (×, ÷, ≠, √, ², ³, ½, →, ∠, △, ⟂). Separate lines for equations. NEVER use LaTeX (no backslash-parentheses, no dollar signs, no backslash commands). Solve the problem. User's request: {prompt}", img_data, caption=caption)
                            else:
                                # No /solve: silently extract text to context, no reply
                                extracted = ask_llm_vision(chat_id, "Read all text in this image. If the image has Hindi/Devanagari text, read and preserve it exactly. Return just the extracted text content, nothing else.", img_data, caption=caption)
                                if chat_id not in conversations:
                                    conversations[chat_id] = []
                                conversations[chat_id].append({"role": "user", "content": f"[Image sent: {caption or 'photo'}]"})
                                conversations[chat_id].append({"role": "assistant", "content": f"[Image text extracted]: {extracted[:500]}"})
                                resp = None  # skip sending a reply
                        except Exception as e:
                            resp = f"Error processing image: {e}"
                    else:
                        log.info(f"From {chat_id} ({user_profiles[chat_id]['name']}): {prompt[:60]}")
                        resp = agent_process(chat_id, prompt)
                    # Send response as reply to original message (skip if None = silent extraction)
                    if resp is not None:
                        send_message(api, chat_id, resp, reply_to_message_id=msg_id)
                        reply_text = resp[:200]
                    else:
                        reply_text = "[Silent image extraction]"
                    # Track user context
                    if uid:
                        user_context[uid] = {"last_topic": text[:200] if text else (caption or "")[:200], "last_msg": text[:200] if text else "", "last_reply": reply_text, "timestamp": time.time()}
                    message_log.append({
                        "chat_id": chat_id,
                        "name": clean_name(msg.get('from', {}).get('first_name', '?')),
                        "username": msg.get('from', {}).get('username', ''),
                        "text": (f"[Photo] {caption[:200]}" if is_photo else text)[:300],
                        "time": int(time.time()),
                        "role": "user"
                    })
                    message_log.append({
                        "chat_id": chat_id,
                        "name": "SAOM",
                        "username": "",
                        "text": reply_text,
                        "time": int(time.time()),
                        "role": "bot"
                    })
                    if len(message_log) > 2000:
                        del message_log[:-1000]
                    global save_counter
                    save_counter += 1
                    if save_counter % 10 == 0:
                        _save_state()
                except Exception as e:
                    log.error(f"Error handling update {upd.get('update_id')}: {e}")
                    continue
        except Exception as e:
            if '409' in str(e):
                log.error(
                    "CONFLICT (409) from Telegram getUpdates: another process is "
                    "ALREADY polling this bot token right now. This means two instances "
                    "are running simultaneously (e.g. an old Render deploy still alive "
                    "during a rolling restart, a scaled service with >1 instance, or the "
                    "bot also running somewhere else with the same SAOM_BOT_TOKEN). "
                    "While both are up, replies will randomly come from whichever process "
                    "wins each poll, which looks exactly like inconsistent/broken behavior. "
                    "Check the Render dashboard for duplicate/leftover services and instance count."
                )
            else:
                log.error(f"Poll error: {e}")
            time.sleep(5)

# ── Health server ──
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'status': 'ok'}).encode())
    def log_message(self, *a): pass

def main():
    log.info(f"SAOM starting | build={BUILD_TAG} | port={PORT} | model={MODEL} | vision_model={VISION_MODEL}")
    if not BOT_TOKEN or not GROQ_KEY:
        log.error("Missing BOT_TOKEN or GROQ_KEY")
        sys.exit(1)
    _restore_state()
    _load_auth()

    def _handle_shutdown(signum, frame):
        log.info(f"Received signal {signum}, saving state before exit...")
        _save_state()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    t = threading.Thread(target=poll, daemon=True)
    t.start()
    HTTPServer(('0.0.0.0', PORT), HealthHandler).serve_forever()

if __name__ == '__main__':
    main()
