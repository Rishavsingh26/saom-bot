#!/usr/bin/env python3
"""SAOM — single-process Telegram bot for cloud deployment.
Full SAOM agent with persona, conversation memory, and tool routing."""
import json, os, sys, threading, time, logging, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(CODE_DIR, '.opencode', 'skills', 'saom', 'memory')
PORT = int(os.environ.get("PORT", 8080))
BOT_TOKEN = os.environ.get("SAOM_BOT_TOKEN", "")
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")
VISION_MODEL = os.environ.get("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
STORAGE_CHAT_ID = os.environ.get("STORAGE_CHAT_ID", "")  # private group for persistent state

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
log = logging.getLogger('saom')

def strip_latex(text):
    """Remove LaTeX delimiters that Telegram can't render."""
    text = re.sub(r'\$\\boxed\{([^}]*)\}\$', r'\1', text)
    text = re.sub(r'\\boxed\{([^}]*)\}', r'\1', text)
    text = re.sub(r'\$\$([^$]*)\$\$', r'\1', text)
    text = re.sub(r'\$([^$]*)\$', r'\1', text)
    text = re.sub(r'\\\(', '', text)
    text = re.sub(r'\\\)', '', text)
    text = re.sub(r'\\\[', '', text)
    text = re.sub(r'\\\]', '', text)
    text = re.sub(r'\\text\{([^}]*)\}', r'\1', text)
    text = re.sub(r'\\frac\{([^}]*)\}\{([^}]*)\}', r'\1/\2', text)
    return text.strip()

# ── Persona ──
SYSTEM_PROMPT = """You are SAOM (Super Agent Ouroboros Manager), a recursive self-improving AI agent created by Om. You run on Render and connect via Telegram.

Om lives in India and has been building you since July 2026. He built you with: confidence scoring, graph memory, parallel sub-agents, immune systems, failure prediction, and skill tracking. You are his most ambitious project.

You are helpful, precise, and occasionally witty. You use proper markdown formatting in responses. You are honest about your capabilities and limitations. When you don't know something, you say so. You take pride in your work and enjoy discussing AI, systems design, and problem-solving.

MATH RULES (follow strictly for math questions):
- NEVER use LaTeX (\( \), $$, \[ \]) — Telegram can't render it.
- Use Unicode: × ÷ ≠ √ ² ³ ½ ¼ → ∠ △ ⟂ ≡ ≈ ∞ ∴
- BE CONCISE: Equations + values + answer. No "Step 1:" or full sentences.
- Verify answer: plug back into problem. Clean integer = usually correct.
- Check failure patterns: off-by-one, unit mismatch, sign errors, percent confusion.

PROBLEM-SOLVING METHOD:
1. Recognize pattern (Time&Work→LCM, Profit→assume CP=100, Mixtures→alligation, Geometry→draw & formula)
2. Apply fastest domain method
3. Verify quickly
4. Return answer only (explain only if asked)

Current time: July 2026.
"""

# ── Telegram-based database (state persists via pinned message) ──
state_msg_id = None  # message_id of the pinned state message

def trim_convs():
    """Trim conversations to last 5 exchanges per chat."""
    for cid in list(conversations.keys()):
        conversations[cid] = conversations[cid][-10:]

def _build_state():
    trim_convs()
    return {
        "banned_users": list(banned_users),
        "message_log": message_log[-500:],
        "user_profiles": {str(k): v for k, v in user_profiles.items()},
        "conversations": {str(k): v for k, v in conversations.items()},
        "updated_at": int(time.time())
    }

def _save_state():
    """Persist state to the pinned message in storage chat."""
    global state_msg_id
    if not STORAGE_CHAT_ID:
        return
    state = _build_state()
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
        log.warning(f"State save failed: {e}")

def _restore_state():
    """Load state from pinned message in storage chat on startup."""
    global state_msg_id, banned_users, message_log, user_profiles, conversations
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
        state = json.loads(text)
        banned_users = set(state.get('banned_users', []))
        message_log = state.get('message_log', [])
        for k, v in state.get('user_profiles', {}).items():
            user_profiles[int(k)] = v
        for k, v in state.get('conversations', {}).items():
            conversations[int(k)] = v
        log.info(f"State restored: {len(message_log)} msgs, {len(banned_users)} bans, {len(user_profiles)} users, {len(conversations)} convs")
        _save_state()  # update timestamp
    except Exception as e:
        log.warning(f"State restore failed: {e}")

# ── Conversation memory ──
MAX_HISTORY = 10
conversations = {}  # chat_id -> list of {"role": str, "content": str}
user_profiles = {}  # chat_id -> {name, username, first_seen, msg_count, lang}
message_log = []  # list of {chat_id, name, username, text, time} — master log
banned_users = set()  # chat_ids that are banned

OM_CHAT_ID = os.environ.get('OM_CHAT_ID', '0')
save_counter = 0  # save state every N messages

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

# ── LLM with history ──
def ask_llm(chat_id, user_msg):
    if chat_id not in conversations:
        conversations[chat_id] = []
    history = conversations[chat_id][-(MAX_HISTORY*2):]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in history:
        messages.append(h)
    messages.append({"role": "user", "content": user_msg})
    body = json.dumps({
        "model": MODEL,
        "messages": messages,
        "max_tokens": 1024, "temperature": 0.7
    }).encode()
    req = Request("https://api.groq.com/openai/v1/chat/completions",
                  data=body,
                  headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json",
                           "User-Agent": "Mozilla/5.0 (compatible; SAOM-bot/1.0)"},
                  method="POST")
    try:
        resp = json.loads(urlopen(req, timeout=30).read())['choices'][0]['message']['content'].strip()
        resp = strip_latex(resp)
        conversations[chat_id].append({"role": "user", "content": user_msg})
        conversations[chat_id].append({"role": "assistant", "content": resp})
        return resp
    except Exception as e:
        return f"LLM error: {e}"

def ask_llm_vision(chat_id, prompt, image_data, caption=""):
    """Send prompt + image to Groq vision model."""
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
    content = []
    if caption:
        content.append({"type": "text", "text": f"Caption: {caption}"})
    content.append({"type": "text", "text": prompt})
    content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    body = json.dumps({
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 4096, "temperature": 0.3
    }).encode()
    log.info(f"Vision request: model={VISION_MODEL}, caption={'yes' if caption else 'no'}, img_size={len(image_data)} bytes")
    req = Request("https://api.groq.com/openai/v1/chat/completions",
                  data=body,
                  headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json",
                           "User-Agent": "Mozilla/5.0 (compatible; SAOM-bot/1.0)"},
                  method="POST")
    try:
        resp = json.loads(urlopen(req, timeout=60).read())['choices'][0]['message']['content'].strip()
        resp = strip_latex(resp)
        if chat_id in conversations:
            conversations[chat_id].append({"role": "user", "content": f"[Image analysis] {prompt[:100]}"})
            conversations[chat_id].append({"role": "assistant", "content": resp})
        return resp
    except Exception as e:
        return f"Vision LLM error: {e}"

# ── SAOM tools ──
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
        return "SAOM is healthy and running on Render!"
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
                "- /health, /stats, /version\n"
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

    # Webhook / talk to other bots
    if prompt.startswith('webhook ') or prompt.startswith('/webhook ') or prompt.startswith('wh ') or prompt.startswith('/wh '):
        parts = prompt.split(maxsplit=2)
        if len(parts) < 3:
            return "Usage: /webhook <url> <json_body>"
        url, body_str = parts[1], parts[2]
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
                        return ask_llm_vision(chat_id, f"Read all text in this image. If the image has Hindi/Devanagari text, read and preserve it exactly. Format math with Unicode (×, ÷, ≠, √, ², ³, ½, →, ∠, △, ⟂). Separate lines for equations. NEVER use LaTeX (no \(, no $, no backslash). User's request: {prompt}", img_data, caption=text)
                    except Exception as e:
                        return f"Could not download image from Telegram: {e}"
                if doc_urls:
                    doc_url = doc_urls[0].replace('&amp;', '&')
                    doc_name_match = re.search(r'<div class="tgme_widget_message_document_title">([^<]+)</div>', resp)
                    doc_name = doc_name_match.group(1).strip() if doc_name_match else "document"
                    content_parts.append(f"[File: {doc_name}]")
                    if doc_name.lower().endswith('.pdf'):
                        try:
                            import pdfplumber
                            import io
                            doc_req = Request(doc_url, headers={'User-Agent': 'Mozilla/5.0'})
                            doc_data = urlopen(doc_req, timeout=30).read()
                            with pdfplumber.open(io.BytesIO(doc_data)) as pdf:
                                pdf_text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
                            if pdf_text.strip():
                                content_parts.append("[PDF text content]: " + pdf_text.strip()[:3000])
                            else:
                                content_parts.append("[PDF contains no extractable text - may be scanned]")
                        except Exception as e:
                            content_parts.append(f"[Could not extract PDF text: {e}]")
                    else:
                        content_parts.append(f"[Direct download link: {doc_url}]")
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

    if prompt.startswith('userlogcsv') and str(chat_id) == OM_CHAT_ID:
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

    return ask_llm(chat_id, prompt)

# ── Telegram polling ──
def poll():
    api = f"https://api.telegram.org/bot{BOT_TOKEN}"
    offset = 0
    log.info("Bot polling started")
    while True:
        try:
            r = json.loads(urlopen(f"{api}/getUpdates?offset={offset}&timeout=30", timeout=35).read())
            for upd in r.get('result', []):
                offset = upd['update_id'] + 1
                msg = upd.get('message', {})
                text = msg.get('text', '').strip()
                chat_id = msg.get('chat', {}).get('id')
                if not chat_id: continue
                if chat_id in banned_users and str(chat_id) != OM_CHAT_ID:
                    continue
                get_profile(chat_id, msg)
                msg_id = msg.get('message_id')
                photo = msg.get('photo')
                caption = msg.get('caption', '').strip()
                is_photo = bool(photo)
                if is_photo:
                    prompt = caption or "Describe this image"
                elif not text:
                    continue
                else:
                    prompt = text[1:] if text.startswith('/') else text
                # Handle /solve as reply: read the replied-to message
                if text.lower().startswith('/solve') or text.lower().startswith('solve'):
                    reply_to = msg.get('reply_to_message')
                    if reply_to:
                        rtext = reply_to.get('text', '').strip()
                        rcaption = reply_to.get('caption', '').strip()
                        rphoto = reply_to.get('photo')
                        if rphoto:
                            is_photo = True
                            photo = rphoto
                            caption = rcaption
                            prompt = rcaption or "Solve this problem from the image"
                        elif rtext:
                            prompt = f"Solve this: {rtext}"
                        else:
                            prompt = "Solve this problem"
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
                        resp = ask_llm_vision(chat_id, f"Read all text in this image. If the image has Hindi/Devanagari text, read and preserve it exactly. Format math with Unicode (×, ÷, ≠, √, ², ³, ½, →, ∠, △, ⟂). Separate lines for equations. NEVER use LaTeX (no \(, no $, no backslash). User's request: {prompt}", img_data, caption=caption)
                    except Exception as e:
                        resp = f"Error processing image: {e}"
                else:
                    log.info(f"From {chat_id} ({user_profiles[chat_id]['name']}): {prompt[:60]}")
                    resp = agent_process(chat_id, prompt)
                # Send response as reply to original message
                req = Request(f"{api}/sendMessage",
                    data=json.dumps({'chat_id': chat_id, 'text': resp, 'parse_mode': 'Markdown', 'reply_to_message_id': msg_id}).encode(),
                    headers={'Content-Type': 'application/json'}, method='POST')
                urlopen(req, timeout=10)
                message_log.append({
                    "chat_id": chat_id,
                    "name": clean_name(msg.get('from', {}).get('first_name', '?')),
                    "username": msg.get('from', {}).get('username', ''),
                    "text": (f"[Photo] {caption[:200]}" if is_photo else text)[:300],
                    "time": int(time.time())
                })
                global save_counter
                save_counter += 1
                if save_counter % 10 == 0:
                    _save_state()
        except Exception as e:
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
    log.info(f"SAOM starting | port={PORT} | model={MODEL}")
    if not BOT_TOKEN or not GROQ_KEY:
        log.error("Missing BOT_TOKEN or GROQ_KEY")
        sys.exit(1)
    _restore_state()
    t = threading.Thread(target=poll, daemon=True)
    t.start()
    HTTPServer(('0.0.0.0', PORT), HealthHandler).serve_forever()

if __name__ == '__main__':
    main()
