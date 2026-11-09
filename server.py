"""Local server for the alien booth page.

It serves the page and the model, and it keeps the OpenRouter API key private: the key lives only in .env (or the
OPENROUTER_API_KEY environment variable), this server adds it to requests it makes to OpenRouter, and it never sends it to the browser.

    python alien-talk/server.py            (then open http://127.0.0.1:8773/)

Only the loopback address is served, other websites cannot call it, and the model and reply length are fixed here,
so a page cannot spend more than a short reply per message.
"""
import http.client, json, os, sys, threading, time
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
MAX_TOKENS = int(ENV.get("OPENROUTER_MAX_TOKENS", "3000"))        # room for a thinking model's hidden thinking as well as its short answer
EFFORT = ENV.get("OPENROUTER_REASONING_EFFORT", "low")            # how much a thinking model may think first: low (about 5 s) still judges tricky messages well; medium is slower, "none" is fast but misreads them
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
        if path == "/api/warm":
            warm()
            return self.reply(200, {"ok": True})
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
        # The fixed part of the prompt (who he is, the symbols, how to answer) is the same every time, so it is sent as a cached system
        # message: after the first message the provider reads it from its cache, which costs about a tenth. The conversation follows it.
        cut = prompt.find("\nYOUR DICTIONARY SO FAR")
        messages = ([{"role": "system", "content": [{"type": "text", "text": prompt[:cut], "cache_control": {"type": "ephemeral"}}]},
                     {"role": "user", "content": prompt[cut + 1:]}] if cut > 0 else [{"role": "user", "content": prompt}])
        payload = {"models": [MODEL] + ([FALLBACK] if FALLBACK and FALLBACK != MODEL else []), "max_tokens": MAX_TOKENS, "temperature": 1.0, "messages": messages}
        if EFFORT == "none":
            payload["reasoning"] = {"enabled": False}
        elif EFFORT:
            payload["reasoning"] = {"effort": EFFORT}                  # how much a thinking model may think before it answers
        payload["stream"] = True                                   # his words come back as they are written, so the page can show them at once
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")   # plain UTF-8: Arabic as 2 bytes a letter instead of a 6-byte escape
        headers = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json", "X-Title": "Talk to the Alien"}
        started = False                                          # once words have gone to the page, a failure can no longer be retried quietly
        try:
            for attempt in (1, 2):                               # one quiet retry: a busy provider or a dropped connection often clears at once
                try:
                    with upstream_conn() as conn:
                        conn.request("POST", "/api/v1/chat/completions", body=body, headers=headers)
                        r = conn.getresponse()
                        if r.status != 200:
                            raw = r.read()
                            if attempt == 1 and r.status in (429, 500, 502, 503, 504, 529):
                                time.sleep(1.0)
                                continue
                            detail = ""
                            try:
                                detail = (json.loads(raw).get("error") or {}).get("message", "")[:160]
                            except Exception:
                                pass
                            return self.line({"error": detail or "OpenRouter refused the request", "status": r.status})
                        for raw in r:                            # server-sent events: "data: {...}" lines, ": comment" keep-alives, "data: [DONE]"
                            raw = raw.strip()
                            if not raw.startswith(b"data:"):
                                continue
                            data = raw[5:].strip()
                            if data == b"[DONE]":
                                break
                            try:
                                ev = json.loads(data)
                            except ValueError:
                                continue
                            if ev.get("error"):
                                if started:
                                    self.line({"error": str((ev["error"] or {}).get("message", "error"))[:160], "status": 502})
                                    return
                                raise ConnectionError("error before the answer")
                            delta = ((ev.get("choices") or [{}])[0].get("delta") or {}).get("content")
                            if delta:
                                if not started:
                                    started = True
                                    self.stream_start()
                                self.line({"d": delta})
                        r.read()                                 # finish the response so the connection can be used again
                    if started:
                        return
                    if attempt == 1:                             # OpenRouter sometimes answers "ok" with nothing inside: ask once more
                        continue
                    return self.line({"error": "empty answer", "status": 502})
                except (BrokenPipeError, ConnectionAbortedError):
                    return                                       # the page gave up (its safety timer ran out): nothing to send
                except Exception:
                    drop_conn()
                    if started:
                        return self.line({"error": "the connection to OpenRouter broke", "status": 0})
                    if attempt == 1:
                        continue                                 # a stale kept-open connection: open a fresh one straight away
                    return self.line({"error": "could not reach OpenRouter", "status": 0})
        except (BrokenPipeError, ConnectionAbortedError):
            drop_conn()

    def stream_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.streaming = True

    def line(self, obj):                                         # one JSON object per line: {"d": words} while he talks, {"error": ...} if it fails
        if not getattr(self, "streaming", False):
            self.stream_start()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
        self.wfile.flush()


# One connection to OpenRouter is kept open and reused, so a message does not pay for a new DNS lookup, TCP and TLS handshake each
# time (on slow venue internet that is about a second). /api/warm opens it while the visitor is still choosing symbols.
CONNECT_TIMEOUT, READ_TIMEOUT = 8, 20                         # give up quickly on a dead network instead of hanging for 40 seconds
up_lock, up = threading.Lock(), {"conn": None, "used": 0.0}


def drop_conn():
    c, up["conn"] = up["conn"], None
    if c:
        try:
            c.close()
        except Exception:
            pass


class upstream_conn:
    """The kept-open connection while it is free; if another request is using it, a one-off connection."""
    def __enter__(self):
        self.shared = up_lock.acquire(timeout=1)
        if self.shared and up["conn"] is not None and time.time() - up["used"] > 50:
            drop_conn()                                          # idle a long time: the far end has probably closed it
        if self.shared and up["conn"] is not None:
            return up["conn"]
        c = http.client.HTTPSConnection("openrouter.ai", timeout=CONNECT_TIMEOUT)
        try:
            c.connect()
            c.sock.settimeout(READ_TIMEOUT)
        except Exception:
            if self.shared:
                up_lock.release()
            raise
        if self.shared:
            up["conn"] = c
        self.own = c
        return c

    def __exit__(self, kind, *rest):
        if self.shared:
            up["used"] = time.time()
            if kind:
                drop_conn()
            up_lock.release()
        elif getattr(self, "own", None):
            self.own.close()
        return False


def warm():
    """Open (or refresh) the kept-open connection with a tiny free request, so the next message starts at once."""
    if not KEY or time.time() - up["used"] < 20:
        return
    try:
        with upstream_conn() as conn:
            conn.request("GET", "/api/v1/key", headers={"Authorization": "Bearer " + KEY})
            conn.getresponse().read()
    except Exception:
        pass

if __name__ == "__main__":
    print(f"Alien booth on http://127.0.0.1:{PORT}/  ({'hidden OpenRouter key loaded' if KEY else 'no key found in .env: simple brain only'})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
