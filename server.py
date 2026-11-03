"""PolyGo API: one persistent Claude session behind a small web service."""
import asyncio, hmac, io, os, re, time
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel
import segno
import corrections
import devices
import rooms
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
SCRIPT_CHARS = re.compile("[" + chr(0x0980) + "-" + chr(0x09FF) + "]")      # the Bengali-Assamese script block (U+0980 to U+09FF)
PASSCODE = _read_passcode()     # kept out of git; if missing, the API refuses to work (fail closed)
fail_lock = asyncio.Lock()


def _passcode_ok(sent):
    return bool(PASSCODE) and hmac.compare_digest(sent.encode("utf-8"), PASSCODE.encode("utf-8"))


def _device_from(request):
    return devices.authenticate(request.cookies.get(devices.COOKIE, "") or request.headers.get("x-device", ""))


async def _slow_no():
    async with fail_lock:           # wrong guesses are serialised: at most one per second overall
        await asyncio.sleep(1)


async def require_auth(request: Request):
    """Only a phone that an admin set up (its device key) gets in. The family passcode opens nothing here:
    it is used once, to claim the very first admin (see /api/claim)."""
    if _device_from(request):
        return
    await _slow_no()
    raise HTTPException(401, "this phone is not set up")


async def require_admin(request: Request):
    d = _device_from(request)
    if d is None:
        await _slow_no()
        raise HTTPException(401, "this phone is not set up")
    if not d.get("admin"):
        raise HTTPException(403, "only an admin can do this")
    return d


def _give_key(response, device, key):
    """The key lives in a cookie the page's scripts cannot read; it is only sent to this site."""
    age = 3 * 365 * 86400
    if device.get("expires_at"):
        age = max(60, int(device["expires_at"] - time.time()))
    response.set_cookie(devices.COOKIE, key, max_age=age, httponly=True, secure=True, samesite="strict", path="/")


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

    async def _run(self, fn):
        """Run fn(session) on the shared Claude session. The caller must hold self.lock."""
        if self.s is None:
            self.s = translator.Session()
            self.restarts += 1
        try:
            r = await asyncio.wait_for(asyncio.to_thread(fn, self.s), TIMEOUT)
        except Exception:
            self._kill()
            raise
        self.turns += 1
        if self.turns >= MAX_TURNS:
            self._kill()
        return r

    async def _call(self, a, b, text, script=False, tone="auto", latin_only=False, also_script=False):
        return await self._run(lambda s: s.translate_pair(a, b, text, script, tone, latin_only, also_script))

    async def room_translate(self, src, targets, text, tone):
        """One room message into every target language at once. Returns (translations, assamese_script, ok)."""
        async with self.lock:
            for strict in (False, True):    # the second pass only runs if Latin Assamese came back with stray non-Latin characters
                if len(targets) == 1:
                    tgt = targets[0]
                    res = await self._run(lambda s: s.translate_to(src, tgt, text, tone, also_script=True, latin_only=strict))
                    ok, n = res["ok"], res["n"]
                    tr = {tgt: res["t"]} if ok else {"English": res["t"]}     # not sure: the English meaning
                else:
                    res = await self._run(lambda s: s.translate_multi(src, targets, text, tone, latin_only=strict))
                    tr, ok, n = res["tr"], res["ok"], res["n"]
                    if ok and any(not tr.get(t) for t in targets):
                        ok = False                                             # the model skipped a language
                if not ok or not tr.get("Assamese") or not NON_ASCII.search(tr["Assamese"]):
                    break
            if ok and tr.get("Assamese") and NON_ASCII.search(tr["Assamese"]):
                ok = False
            if not (ok and isinstance(n, str) and SCRIPT_CHARS.search(n) and len(n) <= 1200):
                n = None
            return tr, n, ok

    async def translate(self, a, b, text, script=False, tone="auto", latin_only=False, also_script=False):
        async with self.lock:
            return await self._call(a, b, text, script, tone, latin_only, also_script)

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
    sweeper = asyncio.create_task(rooms.sweep_forever())
    yield
    sweeper.cancel()
    engine._kill()


# No auto-generated API docs: the app is reachable from the internet, so don't describe it to strangers.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(devices.DeviceError)
async def device_error(_request, e):
    return JSONResponse({"detail": e.detail}, status_code=e.status)


@app.exception_handler(rooms.RoomError)
async def room_error(_request, e):
    return JSONResponse({"detail": e.detail}, status_code=e.status)


class Req(BaseModel):
    a: str      # the two languages of the conversation
    b: str
    text: str
    script: bool = False    # write Assamese output in Assamese script instead of Latin letters
    tone: str = "formal"    # formal (apuni/Sie), informal (tumi/du) or auto (mirror the speaker)
    speak_script: bool = False  # also return the Assamese in script, for a phone with an Assamese/Bengali voice


class Claim(BaseModel):
    passcode: str
    name: str
    lang: str


class Profile(BaseModel):
    lang: str


class Redeem(BaseModel):
    token: str


class InviteNew(BaseModel):
    name: str
    lang: str
    tone: str = "formal"
    admin: bool = False
    access_days: int = 0
    link_hours: float = 0


@app.get("/api/me")
async def api_me(request: Request):
    """Who is this phone? Public on purpose: a phone that is not set up learns whether it may claim the admin place."""
    d = _device_from(request)
    if d:
        return {"enrolled": True, "device": devices.public(d)}
    return {"enrolled": False, "can_claim": not devices.have_admin()}


@app.post("/api/me/profile")
async def api_me_profile(p: Profile, request: Request):
    """A phone changes its own language (page and speech); nothing else about it."""
    d = _device_from(request)
    if d is None:
        await _slow_no()
        raise HTTPException(401, "this phone is not set up")
    return {"device": devices.public(devices.set_lang(d["id"], p.lang))}


@app.post("/api/claim")
async def api_claim(c: Claim, response: Response):
    """The very first admin: needs the family passcode, and works only while the server has no admin."""
    if devices.have_admin():
        raise HTTPException(409, "this server already has an admin")
    if not PASSCODE:
        raise HTTPException(503, "no passcode is set up on the server")
    if not _passcode_ok(c.passcode):
        await _slow_no()
        raise HTTPException(401, "wrong passcode")
    d, key = devices.claim_admin(c.name, c.lang)
    _give_key(response, d, key)
    return {"enrolled": True, "device": devices.public(d)}


@app.post("/api/invite/redeem")
async def api_redeem(r: Redeem, response: Response):
    try:
        d, key = devices.redeem(r.token)
    except devices.DeviceError:
        await _slow_no()
        raise
    _give_key(response, d, key)
    return {"enrolled": True, "device": devices.public(d)}


@app.post("/api/logout")
async def api_logout(response: Response):
    response.delete_cookie(devices.COOKIE, path="/")
    return {"ok": True}


@app.post("/api/admin/invites")
async def admin_invite(i: InviteNew, admin=Depends(require_admin)):
    inv, token = devices.create_invite(admin, i.name, i.lang, i.tone, i.admin, i.access_days, i.link_hours)
    return {"token": token, "invite": devices.public_invite(inv)}


@app.get("/api/admin/devices")
async def admin_devices(admin=Depends(require_admin)):
    return {**devices.listing(), "me": admin["id"]}


@app.delete("/api/admin/devices/{did}")
async def admin_forget(did: str, admin=Depends(require_admin)):
    return {"removed": devices.forget(did, admin)}


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
        res = await engine.translate(r.a, r.b, text, r.script, r.tone, also_script=r.speak_script)
    except Exception as e:
        raise HTTPException(503, f"translator failed ({type(e).__name__}); try again")
    ok, frm = res["ok"], res["from"]
    if ok and frm not in (r.a, r.b):
        ok = False                  # model said ok but did not name a valid source language
    to = (r.b if frm == r.a else r.a) if ok else "English"
    if ok and to == "Assamese" and not r.script and NON_ASCII.search(res["t"]):
        # Latin-letter Assamese must be plain ASCII; a stray Devanagari/Bengali character means a glitch.
        try:
            res = await engine.translate(r.a, r.b, text, r.script, r.tone, latin_only=True,
                                         also_script=r.speak_script)
            ok, frm = res["ok"] and res["from"] in (r.a, r.b), res["from"]
            to = (r.b if frm == r.a else r.a) if ok else "English"
        except Exception:
            ok, to = False, "English"
        if ok and to == "Assamese" and NON_ASCII.search(res["t"]):
            ok, to = False, "English"   # still broken: show it as "not sure" rather than as fact
    n = res.get("n")                # Assamese script for speech; only trusted if it really is script
    if not (ok and to == "Assamese" and not r.script and r.speak_script and isinstance(n, str)
            and SCRIPT_CHARS.search(n) and len(n) <= 1200):
        n = None
    return {"t": res["t"], "n": n, "ok": ok, "from": frm if frm in (r.a, r.b) else None, "to": to,
            "ms": int((time.time() - t0) * 1000)}


class Correction(BaseModel):
    frm: str                # language of the original text
    to: str                 # language of the translation
    tone: str = "auto"
    form: str = ""          # "Latin letters" / "Assamese script" when the translation is Assamese
    text: str               # what was said
    wrong: str = ""         # what PolyGo answered
    right: str              # what it should have said
    note: str = ""


@app.post("/api/correct", dependencies=[Depends(require_auth)])
async def api_correct(c: Correction):
    if c.frm not in LANGS or c.to not in LANGS or c.frm == c.to:
        raise HTTPException(400, "bad language pair")
    if c.tone not in ("formal", "informal", "auto"):
        raise HTTPException(400, "bad tone")
    try:
        entry, count = corrections.add(c.frm, c.to, c.tone, c.form, c.text, c.wrong, c.right, c.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await engine.reset()            # a fresh session reads the new correction
    return {"ok": True, "id": entry["id"], "count": count}


@app.get("/api/corrections", dependencies=[Depends(require_auth)])
async def api_corrections():
    return {"items": corrections.listing(30), "count": corrections.count()}


@app.delete("/api/corrections/{cid}", dependencies=[Depends(require_auth)])
async def api_delete_correction(cid: str):
    if not corrections.remove(cid):
        raise HTTPException(404, "no such correction")
    await engine.reset()
    return {"ok": True}


class RoomNew(BaseModel):
    name: str = ""
    lang: str
    tone: str = "formal"
    max_size: int = rooms.DEFAULT_SIZE


class RoomJoin(BaseModel):
    name: str = ""
    lang: str
    tone: str = "formal"
    pid: str = ""           # set when coming back after a reload


class RoomMsg(BaseModel):
    pid: str
    text: str


class RoomWho(BaseModel):
    pid: str


class RoomProfile(BaseModel):
    pid: str
    lang: str = ""
    tone: str = ""
    name: str = ""


class RoomSize(BaseModel):
    pid: str
    max_size: int


@app.post("/api/rooms", dependencies=[Depends(require_auth)])
async def room_new(r: RoomNew):
    """Start a shared conversation; the creator is its first member."""
    room = rooms.create(r.max_size)
    try:
        p = await room.join(r.name, r.lang, r.tone)
    except Exception:
        rooms.ROOMS.pop(room.id, None)
        raise
    return {"room": room.id, "pid": p.pid, "name": p.name, "since": p.joined_event, "ceiling": rooms.ROOM_CEILING,
            **room.summary()}


@app.get("/api/rooms/{rid}", dependencies=[Depends(require_auth)])
async def room_info(rid: str):
    room = rooms.get(rid)
    return {"ceiling": rooms.ROOM_CEILING, "full": len(room.active()) >= room.max_size, **room.summary()}


@app.post("/api/rooms/{rid}/join", dependencies=[Depends(require_auth)])
async def room_join(rid: str, r: RoomJoin):
    room = rooms.get(rid)
    p = await room.join(r.name, r.lang, r.tone, r.pid)
    return {"pid": p.pid, "name": p.name, "since": p.joined_event, "creator": p.pid == room.creator,
            "ceiling": rooms.ROOM_CEILING, **room.summary()}


@app.get("/api/rooms/{rid}/events", dependencies=[Depends(require_auth)])
async def room_events(rid: str, pid: str = "", since: int = 0, wait: float = 20.0):
    return await rooms.get(rid).poll(pid, since, wait)


@app.post("/api/rooms/{rid}/messages", dependencies=[Depends(require_auth)])
async def room_send(rid: str, m: RoomMsg):
    room = rooms.get(rid)
    p = room.person(m.pid)
    text = re.sub(r"\s+", " ", m.text).strip()
    room.check_send(p, text)
    targets = room.languages_for(p)             # every other language anyone in the room needs (asleep or not)
    t0 = time.time()
    tr, n, ok = {}, None, True
    if targets:
        room.pending += 1
        try:
            tr, n, ok = await engine.room_translate(p.lang, targets, text, p.tone)
        except Exception as e:
            raise HTTPException(503, f"translator failed ({type(e).__name__}); try again")
        finally:
            room.pending -= 1
    ev = await room.post(p, text, tr, n, ok)
    return {"id": ev["id"], "ok": ok, "ms": int((time.time() - t0) * 1000), "languages": targets}


@app.post("/api/rooms/{rid}/profile", dependencies=[Depends(require_auth)])
async def room_profile(rid: str, r: RoomProfile):
    room = rooms.get(rid)
    p = room.person(r.pid)
    if r.lang:
        if r.lang not in rooms.LANGS:
            raise HTTPException(400, "unknown language")
        p.lang = r.lang
    if r.tone:
        if r.tone not in rooms.TONES:
            raise HTTPException(400, "unknown tone")
        p.tone = r.tone
    if rooms.clean_name(r.name):
        p.name = rooms.clean_name(r.name)
    p.last_seen = time.time()
    return room.summary()


@app.post("/api/rooms/{rid}/size", dependencies=[Depends(require_auth)])
async def room_size(rid: str, r: RoomSize):
    room = rooms.get(rid)
    room.resize(room.person(r.pid), r.max_size)
    return room.summary()


@app.post("/api/rooms/{rid}/leave", dependencies=[Depends(require_auth)])
async def room_leave(rid: str, r: RoomWho):
    room = rooms.get(rid)
    await room.leave(room.person(r.pid))
    return {"ok": True}


@app.post("/api/rooms/{rid}/end", dependencies=[Depends(require_auth)])
async def room_end(rid: str, r: RoomWho):
    room = rooms.get(rid)
    await room.end(room.person(r.pid))
    return {"ok": True}


class QR(BaseModel):
    text: str


@app.post("/api/qr", dependencies=[Depends(require_auth)])
async def api_qr(q: QR):
    """Draw a QR code for an invite link here, so the link never goes to an outside QR service."""
    text = q.text.strip()
    if not text.startswith(("http://", "https://")) or len(text) > 800:
        raise HTTPException(400, "the text must be a link of at most 800 characters")
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=8, border=3, dark="#000000", light="#ffffff",
                                     xmldecl=False)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.post("/api/reset", dependencies=[Depends(require_auth)])
async def api_reset():
    await engine.reset()
    return {"ok": True}


@app.get("/api/health")
async def api_health(request: Request):
    if _device_from(request) is None:
        return {"ok": True}         # public: just "the service is up"
    return {"ok": True, "model": translator.MODEL, "turns": engine.turns, "corrections": corrections.count(),
            "restarts": engine.restarts, "engine_running": engine.s is not None}


@app.get("/")
async def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))
