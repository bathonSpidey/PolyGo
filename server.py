"""AutoTranslate API: one persistent Claude session behind a small web service."""
import asyncio, hmac, os, re, time
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import translator

HERE = os.path.dirname(os.path.abspath(__file__))
LANGS = {"Assamese", "German", "English", "Hindi"}
MAX_TURNS = 40      # restart the engine after this many turns (stops context drift/growth)
TIMEOUT = 45        # seconds before a hung call is killed


def _read_passcode():
    path = os.environ.get("POLYGO_PASSCODE_FILE", os.path.join(HERE, "passcode.txt"))
    try:
        return open(path, encoding="utf-8").read().strip()
    except OSError:
        return ""


NON_ASCII = re.compile(r"[^\x00-\x7F]")
PASSCODE = _read_passcode()     # kept out of git; if missing, the API refuses to work (fail closed)
fail_lock = asyncio.Lock()


def _passcode_ok(sent):
    return bool(PASSCODE) and hmac.compare_digest(sent.encode("utf-8"), PASSCODE.encode("utf-8"))


async def require_auth(x_passcode: str = Header(default="")):
    if not PASSCODE:
        raise HTTPException(503, "no passcode is set up on the server")
    if _passcode_ok(x_passcode):
        return
    async with fail_lock:           # wrong guesses are serialised: at most one per second overall
        await asyncio.sleep(1)
    raise HTTPException(401, "wrong passcode")


class Engine:
    def __init__(self):
        self.s = None
        self.turns = 0
        self.lock = asyncio.Lock()
        self.restarts = 0

    def _kill(self):
        if self.s:
            self.s.kill()
        self.s = None
        self.turns = 0

    async def _call(self, a, b, text, script=False, tone="auto", latin_only=False):
        if self.s is None:
            self.s = translator.Session()
            self.restarts += 1
        try:
            r = await asyncio.wait_for(
                asyncio.to_thread(self.s.translate_pair, a, b, text, script, tone, latin_only), TIMEOUT)
        except Exception:
            self._kill()
            raise
        self.turns += 1
        if self.turns >= MAX_TURNS:
            self._kill()
        return r

    async def translate(self, a, b, text, script=False, tone="auto", latin_only=False):
        async with self.lock:
            return await self._call(a, b, text, script, tone, latin_only)

    async def warm(self):
        async with self.lock:
            try:
                await self._call("English", "German", "Hello")
            except Exception:
                pass

    async def reset(self):
        async with self.lock:
            self._kill()
        asyncio.create_task(self.warm())


engine = Engine()


@asynccontextmanager
async def lifespan(app):
    asyncio.create_task(engine.warm())
    yield
    engine._kill()


app = FastAPI(lifespan=lifespan)


class Req(BaseModel):
    a: str      # the two languages of the conversation
    b: str
    text: str
    script: bool = False    # write Assamese output in Assamese script instead of Latin letters
    tone: str = "formal"    # formal (apuni/Sie), informal (tumi/du) or auto (mirror the speaker)


@app.post("/api/auth", dependencies=[Depends(require_auth)])
async def api_auth():
    return {"ok": True}


@app.post("/api/translate", dependencies=[Depends(require_auth)])
async def api_translate(r: Req):
    text = r.text.strip()
    if r.a not in LANGS or r.b not in LANGS or r.a == r.b:
        raise HTTPException(400, "bad language pair")
    if not text or len(text) > 600:
        raise HTTPException(400, "text must be 1-600 characters")
    if r.tone not in ("formal", "informal", "auto"):
        raise HTTPException(400, "bad tone")
    t0 = time.time()
    try:
        res = await engine.translate(r.a, r.b, text, r.script, r.tone)
    except Exception as e:
        raise HTTPException(503, f"translator failed ({type(e).__name__}); try again")
    ok, frm = res["ok"], res["from"]
    if ok and frm not in (r.a, r.b):
        ok = False                  # model said ok but did not name a valid source language
    to = (r.b if frm == r.a else r.a) if ok else "English"
    if ok and to == "Assamese" and not r.script and NON_ASCII.search(res["t"]):
        # Latin-letter Assamese must be plain ASCII; a stray Devanagari/Bengali character means a glitch.
        try:
            res = await engine.translate(r.a, r.b, text, r.script, r.tone, latin_only=True)
            ok, frm = res["ok"] and res["from"] in (r.a, r.b), res["from"]
            to = (r.b if frm == r.a else r.a) if ok else "English"
        except Exception:
            ok, to = False, "English"
        if ok and to == "Assamese" and NON_ASCII.search(res["t"]):
            ok, to = False, "English"   # still broken: show it as "not sure" rather than as fact
    return {"t": res["t"], "ok": ok, "from": frm if frm in (r.a, r.b) else None, "to": to,
            "ms": int((time.time() - t0) * 1000)}


@app.post("/api/reset", dependencies=[Depends(require_auth)])
async def api_reset():
    await engine.reset()
    return {"ok": True}


@app.get("/api/health")
async def api_health(x_passcode: str = Header(default="")):
    if not _passcode_ok(x_passcode):
        return {"ok": True}         # public: just "the service is up"
    return {"ok": True, "model": os.environ.get("AT_MODEL", "haiku"), "turns": engine.turns,
            "restarts": engine.restarts, "engine_running": engine.s is not None}


@app.get("/")
async def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))
