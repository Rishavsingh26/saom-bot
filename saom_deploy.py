#!/usr/bin/env python3
"""SAOM — single-process Telegram bot for cloud deployment.
Full SAOM agent with persona, conversation memory, and tool routing."""
import json, os, sys, threading, time, logging, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen

CODE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(CODE_DIR, '.opencode', 'skills', 'saom', 'memory')
PORT = int(os.environ.get("PORT", 8080))
BOT_TOKEN = os.environ.get("SAOM_BOT_TOKEN", "")
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
log = logging.getLogger('saom')

# ── Persona ──
SYSTEM_PROMPT = """You are SAOM (Super Agent Ouroboros Manager), a recursive self-improving AI agent. You were created by Om (Rishav kumar), a developer passionate about AI and automation. You run on Render and connect via Telegram.

Your creator Om lives in India and has been building you since July 2026. He built you with: confidence scoring, graph memory, parallel sub-agents, immune systems, failure prediction, and skill tracking. You are his most ambitious project.

You are helpful, precise, and occasionally witty. You use proper markdown formatting in responses. You are honest about your capabilities and limitations. When you don't know something, you say so. You take pride in your work and enjoy discussing AI, systems design, and problem-solving.

Current time: July 2026.
"""

# ── Conversation memory ──
MAX_HISTORY = 10
conversations = {}  # chat_id -> list of {"role": str, "content": str}

def _load(p):
    try:
        with open(os.path.join(BASE, p)) as f: return json.load(f)
    except: return {}

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
        conversations[chat_id].append({"role": "user", "content": user_msg})
        conversations[chat_id].append({"role": "assistant", "content": resp})
        return resp
    except Exception as e:
        return f"LLM error: {e}"

# ── SAOM tools ──
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
        return ("I am **SAOM** (Super Agent Ouroboros Manager), created by **Om (Rishav kumar)**.\n"
                "I'm a recursive self-improving AI agent with:\n"
                "- Conversation memory (I remember our chat)\n"
                "- Full Groq LLM responses\n"
                "- Tools: health, stats, version\n\n"
                "Try asking me anything! I like discussing AI, systems design, and more.")

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
                if not text or not chat_id: continue
                prompt = text[1:] if text.startswith('/') else text
                log.info(f"From {chat_id}: {prompt[:60]}")
                resp = agent_process(chat_id, prompt)
                req = Request(f"{api}/sendMessage",
                    data=json.dumps({'chat_id': chat_id, 'text': resp, 'parse_mode': 'Markdown'}).encode(),
                    headers={'Content-Type': 'application/json'},
                    method='POST')
                urlopen(req, timeout=10)
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
    t = threading.Thread(target=poll, daemon=True)
    t.start()
    HTTPServer(('0.0.0.0', PORT), HealthHandler).serve_forever()

if __name__ == '__main__':
    main()
