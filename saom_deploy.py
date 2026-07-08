#!/usr/bin/env python3
"""SAOM — single-process Telegram bot for cloud deployment.
Listens on $PORT for platform health checks, polls Telegram, answers via Groq."""
import json, os, sys, threading, time, logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen

CODE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(CODE_DIR, "saom_config.json")
PORT = int(os.environ.get("PORT", 8080))
BOT_TOKEN = os.environ.get("SAOM_BOT_TOKEN", "")
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")

# Fallback to config file
if not GROQ_KEY and os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH) as f:
        c = json.load(f)
    g = c.get('providers', {}).get('groq', {})
    GROQ_KEY = g.get('api_key', '')
    MODEL = g.get('model', MODEL)

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
log = logging.getLogger('saom')

# ── LLM ──
def ask_llm(prompt):
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1024,
        "temperature": 0.7
    }).encode()
    req = Request("https://api.groq.com/openai/v1/chat/completions",
                  data=body,
                  headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json",
                           "User-Agent": "Mozilla/5.0 (compatible; SAOM-bot/1.0)"},
                  method="POST")
    try:
        resp = json.loads(urlopen(req, timeout=30).read())
        return resp['choices'][0]['message']['content'].strip()
    except Exception as e:
        return f"LLM error: {e}"

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
                if not text or not chat_id:
                    continue
                prompt = text[1:] if text.startswith('/') else text
                log.info(f"From {chat_id}: {prompt[:60]}")
                resp = ask_llm(prompt)
                req = Request(f"{api}/sendMessage",
                    data=json.dumps({'chat_id': chat_id, 'text': resp}).encode(),
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
    log.info(f"SAOM starting | port={PORT} | model={MODEL} | key={'set' if GROQ_KEY else 'MISSING'}")
    if not BOT_TOKEN or not GROQ_KEY:
        log.error("Missing BOT_TOKEN or GROQ_KEY")
        sys.exit(1)
    t = threading.Thread(target=poll, daemon=True)
    t.start()
    HTTPServer(('0.0.0.0', PORT), HealthHandler).serve_forever()

if __name__ == '__main__':
    main()
