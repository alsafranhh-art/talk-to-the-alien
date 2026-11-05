"""Local server for the alien booth page.

It serves the page and the model, and it keeps the OpenRouter API key private: the key lives only in .env (or the
OPENROUTER_API_KEY environment variable), this server adds it to requests it makes to OpenRouter, and it never sends it to the browser.

    python alien-talk/server.py            (then open http://127.0.0.1:8773/)

Only the loopback address is served, other websites cannot call it, and the model and reply length are fixed here,
so a page cannot spend more than a short reply per message.
"""
import json, os, sys, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8773"))
FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
         "/alien.glb": ("alien.glb", "model/gltf-binary")}      # the only files ever served; .env can never be reached


def load_env():
    env = {}
    try:
        with open(os.path.join(HERE, ".env"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return {**env, **{k: v for k, v in os.environ.items() if k.startswith("OPENROUTER_")}}


ENV = load_env()
KEY = ENV.get("OPENROUTER_API_KEY", "")
MODEL = ENV.get("OPENROUTER_MODEL", "anthropic/claude-haiku-5.5")
FALLBACK = ENV.get("OPENROUTER_FALLBACK_MODEL", "google/gemini-2.5-flash-lite")      # used automatically when the main model is busy or rate-limited
MAX_PROMPT, MAX_BODY, PER_MINUTE = 14000, 40000, 40
MAX_TOKENS = int(ENV.get("OPENROUTER_MAX_TOKENS", "1500"))        # room for a thinking model's hidden thinking as well as its short answer
EFFORT = ENV.get("OPENROUTER_REASONING_EFFORT", "low")            # how much a thinking model may think first: low is fast, "" leaves it to the model
recent = []                                                     # times of recent chat calls, to cap spending


class Handler(BaseHTTPRequestHandler):
    server_version = "AlienBooth"

    def log_message(self, *args):                               # no request logging: nothing sensitive ever reaches the console
        pass

    def reply(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def local_only(self):                                       # blocks other sites (and DNS tricks) from using this server
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")

    def do_GET(self):
        if not self.local_only():
            return self.reply(403, {"error": "not allowed"})
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            return self.reply(200, {"proxy": bool(KEY), "model": MODEL})
        if path in FILES:
            name, ctype = FILES[path]
            try:
                with open(os.path.join(HERE, name), "rb") as f:
                    return self.reply(200, f.read(), ctype)
            except OSError:
                pass
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        if not self.local_only() or self.path.split("?", 1)[0] != "/api/chat" or self.headers.get("X-Alien") != "1":
            return self.reply(403, {"error": "not allowed"})
        if not KEY:
            return self.reply(503, {"error": "no key is set on the server", "status": 503})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self.reply(413, {"error": "message too large", "status": 413})
            prompt = json.loads(self.rfile.read(n))["prompt"]
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return self.reply(400, {"error": "bad request", "status": 400})
        now = time.time()
        recent[:] = [t for t in recent if now - t < 60]
        if len(recent) >= PER_MINUTE:
            return self.reply(429, {"error": "too many messages, wait a moment", "status": 429})
        recent.append(now)
        payload = {"models": [MODEL] + ([FALLBACK] if FALLBACK and FALLBACK != MODEL else []), "max_tokens": MAX_TOKENS, "temperature": 1.0, "messages": [{"role": "user", "content": prompt}]}
        if EFFORT:
            payload["reasoning"] = {"effort": EFFORT}                  # how much a thinking model may think before it answers: low is faster
        body = json.dumps(payload).encode()
        req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body, method="POST", headers={
            "Authorization": "Bearer " + KEY, "Content-Type": "application/json", "X-Title": "Talk to the Alien"})
        for attempt in (1, 2):                                  # one quiet retry: a busy provider often clears within a second
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    data = json.load(r)
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                if not text.strip():                            # OpenRouter sometimes answers "ok" with nothing inside: treat it as a failure and try again
                    if attempt == 1:
                        time.sleep(0.8)
                        continue
                    return self.reply(200, {"error": ((data.get("error") or {}).get("message") or "empty answer")[:160], "status": 502})
                return self.reply(200, {"text": text})
            except urllib.error.HTTPError as e:
                if attempt == 1 and e.code in (429, 500, 502, 503, 504, 529):
                    time.sleep(1.2)
                    continue
                detail = ""
                try:
                    detail = (json.load(e).get("error") or {}).get("message", "")[:160]
                except Exception:
                    pass
                return self.reply(200, {"error": detail or "OpenRouter refused the request", "status": e.code})
            except Exception:
                if attempt == 1:
                    time.sleep(1.2)
                    continue
                return self.reply(200, {"error": "could not reach OpenRouter", "status": 0})


if __name__ == "__main__":
    print(f"Alien booth on http://127.0.0.1:{PORT}/  ({'hidden OpenRouter key loaded' if KEY else 'no key found in .env: simple brain only'})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
