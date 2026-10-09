"""AutoTranslate API: one persistent Claude session behind a small web service."""
import asyncio, os, time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import translator

HERE = os.path.dirname(os.path.abspath(__file__))
LANGS = {"Assamese", "German", "English", "Hindi"}
MAX_TURNS = 40      # restart the engine after this many turns (stops context drift/growth)
TIMEOUT = 45        # seconds before a hung call is killed


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

    async def _call(self, a, b, text, script=False):
        if self.s is None:
            self.s = translator.Session()
            self.restarts += 1
        try:
            r = await asyncio.wait_for(
                asyncio.to_thread(self.s.translate_pair, a, b, text, script), TIMEOUT)
        except Exception:
            self._kill()
            raise
        self.turns += 1
        if self.turns >= MAX_TURNS:
            self._kill()
        return r

    async def translate(self, a, b, text, script=False):
        async with self.lock:
            return await self._call(a, b, text, script)

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


@app.post("/api/translate")
async def api_translate(r: Req):
    text = r.text.strip()
    if r.a not in LANGS or r.b not in LANGS or r.a == r.b:
        raise HTTPException(400, "bad language pair")
    if not text or len(text) > 600:
        raise HTTPException(400, "text must be 1-600 characters")
    t0 = time.time()
    try:
        res = await engine.translate(r.a, r.b, text, r.script)
    except Exception as e:
        raise HTTPException(503, f"translator failed ({type(e).__name__}); try again")
    ok, frm = res["ok"], res["from"]
    if ok and frm not in (r.a, r.b):
        ok = False                  # model said ok but did not name a valid source language
    to = (r.b if frm == r.a else r.a) if ok else "English"
    return {"t": res["t"], "ok": ok, "from": frm if frm in (r.a, r.b) else None, "to": to,
            "ms": int((time.time() - t0) * 1000)}


@app.post("/api/reset")
async def api_reset():
    await engine.reset()
    return {"ok": True}


@app.get("/api/health")
async def api_health():
    return {"model": os.environ.get("AT_MODEL", "haiku"), "turns": engine.turns,
            "restarts": engine.restarts, "engine_running": engine.s is not None}


@app.get("/")
async def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))
