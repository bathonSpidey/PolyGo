"""Shared conversations ("rooms"): several phones, each in its own language, one live conversation.

Everything lives in memory only. No message is ever written to disk, a restart ends every room, and a
person who joins late sees nothing from before they joined. Access to a room needs the family passcode
(checked by the API); the room id in the link only says WHICH conversation.
"""
import asyncio, os, re, secrets, time
from collections import deque

LANGS = ("Assamese", "German", "English", "Hindi")
TONES = ("formal", "informal", "auto")
ROOM_TTL = float(os.environ.get("POLYGO_ROOM_TTL_HOURS", "24")) * 3600      # a link works for one day
ROOM_CEILING = max(2, int(os.environ.get("POLYGO_ROOM_MAX", "10")))         # largest allowed conversation
DEFAULT_SIZE = 4
MAX_ROOMS = 50
ACTIVE_WINDOW = float(os.environ.get("POLYGO_ROOM_ACTIVE_SECONDS", "90"))   # seconds a phone counts as present after its last check-in
FORGET_AFTER = 600          # seconds of silence before a phone is dropped from the room
MAX_TEXT = 600
RATE_COUNT, RATE_WINDOW = 8, 30.0   # messages per person per window
MAX_PENDING = 6             # translations waiting in one room before we say "busy"

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class RoomError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


def clean_name(value):
    """One tidy line, at most 30 characters; empty if nothing usable was given (a name is optional)."""
    return re.sub(r"\s+", " ", _CONTROL.sub(" ", str(value or ""))).strip()[:30]


class Participant:
    def __init__(self, name, lang, tone):
        self.pid = secrets.token_urlsafe(9)
        self.name, self.lang, self.tone = name, lang, tone
        self.joined_event = 0
        self.last_seen = time.time()
        self.sent = deque()


class Room:
    def __init__(self, max_size):
        self.id = secrets.token_urlsafe(16)                 # 128 random bits: not guessable
        self.created = time.time()
        self.expires = self.created + ROOM_TTL
        self.max_size = max_size
        self.closed = False
        self.closed_at = 0.0
        self.creator = None
        self.people = {}
        self.events = deque(maxlen=300)
        self.last_id = 0
        self.cond = asyncio.Condition()
        self.pending = 0
        self.empty_since = None

    # ---- state ----
    def alive(self):
        return not self.closed and time.time() < self.expires

    def active(self):
        now = time.time()
        return [p for p in self.people.values() if now - p.last_seen < ACTIVE_WINDOW]

    def person(self, pid):
        p = self.people.get(pid)
        if p is None:
            raise RoomError(404, "you are not in this conversation")
        return p

    def summary(self):
        return {"max_size": self.max_size, "expires_in": max(0, int(self.expires - time.time())),
                "people": [{"name": p.name, "lang": p.lang} for p in self.people.values()],
                "closed": self.closed or time.time() >= self.expires}

    def languages_for(self, sender):
        """Every other language someone IN THE ROOM needs a translation into. This counts everyone who has joined and
        not left, not only phones that checked in a moment ago: a phone that is asleep or in another window must still
        find the translation waiting when it comes back."""
        return sorted({p.lang for p in self.people.values() if p.lang != sender.lang})

    def guest_name(self):
        taken = {p.name for p in self.people.values()}
        n = 1
        while f"Guest {n}" in taken:
            n += 1
        return f"Guest {n}"

    # ---- events ----
    async def publish(self, kind, **data):
        self.last_id += 1
        ev = {"id": self.last_id, "kind": kind, "ts": time.time(), **data}
        self.events.append(ev)
        async with self.cond:
            self.cond.notify_all()
        return ev

    def render(self, ev, p):
        """What THIS person sees of an event: messages come in their own language."""
        kind = ev["kind"]
        if kind in ("join", "leave") and ev.get("pid") == p.pid:
            return None
        if kind != "message":
            return {k: ev[k] for k in ("id", "kind", "ts", "name", "lang") if k in ev}
        out = {"id": ev["id"], "kind": "message", "ts": ev["ts"], "from": ev["name"], "from_lang": ev["lang"],
               "mine": ev["pid"] == p.pid, "original": ev["text"], "ok": ev["ok"], "translated": True}
        if ev["lang"] == p.lang:
            out["text"] = ev["text"]
            out["translated"], out["ok"] = False, True       # nothing was translated for them, so nothing to doubt
        else:
            text = ev["tr"].get(p.lang) or ev["tr"].get("English")
            if text is None:                       # no translation was made for this person: say so, do not pretend
                out["text"], out["translated"], out["untranslated"] = ev["text"], False, True
            else:
                out["text"] = text
                if p.lang == "Assamese" and ev.get("n"):
                    out["n"] = ev["n"]
                if not ev["tr"].get(p.lang):          # only the English fallback exists
                    out["ok"] = False
        return out

    # ---- people ----
    async def join(self, name, lang, tone, pid=""):
        if not self.alive():
            raise RoomError(410, "this conversation has ended or the link has expired")
        if lang not in LANGS:
            raise RoomError(400, "unknown language")
        if tone not in TONES:
            raise RoomError(400, "unknown tone")
        p = self.people.get(pid) if pid else None
        if p:                                        # coming back after a reload or a dropped connection
            p.name, p.lang, p.tone, p.last_seen = clean_name(name) or p.name, lang, tone, time.time()
            return p
        if len(self.active()) >= self.max_size:
            raise RoomError(409, "this conversation is full")
        p = Participant(clean_name(name) or self.guest_name(), lang, tone)
        self.people[p.pid] = p
        if self.creator is None:
            self.creator = p.pid
        ev = await self.publish("join", pid=p.pid, name=p.name, lang=p.lang)
        p.joined_event = ev["id"]                    # they will only ever see what happens after this
        return p

    async def leave(self, p):
        self.people.pop(p.pid, None)
        await self.publish("leave", pid=p.pid, name=p.name, lang=p.lang)

    async def end(self, p):
        if p.pid != self.creator:
            raise RoomError(403, "only the person who started the conversation can end it")
        self.closed, self.closed_at = True, time.time()
        await self.publish("end", name=p.name)

    def resize(self, p, size):
        if p.pid != self.creator:
            raise RoomError(403, "only the person who started the conversation can change its size")
        if not 2 <= size <= ROOM_CEILING:
            raise RoomError(400, f"the size must be between 2 and {ROOM_CEILING}")
        if size < len(self.active()):
            raise RoomError(400, "more people are in the conversation than that")
        self.max_size = size

    # ---- messages ----
    def check_send(self, p, text):
        if not self.alive():
            raise RoomError(410, "this conversation has ended or the link has expired")
        if not text or len(text) > MAX_TEXT:
            raise RoomError(400, f"a message must be 1-{MAX_TEXT} characters")
        now = time.time()
        while p.sent and now - p.sent[0] > RATE_WINDOW:
            p.sent.popleft()
        if len(p.sent) >= RATE_COUNT:
            raise RoomError(429, "too many messages, wait a moment")
        if self.pending >= MAX_PENDING:
            raise RoomError(503, "the translator is busy, try again in a moment")
        p.sent.append(now)

    async def post(self, p, text, tr, n, ok):
        return await self.publish("message", pid=p.pid, name=p.name, lang=p.lang, text=text, tr=tr, n=n, ok=ok)

    async def poll(self, pid, since, wait):
        """Long poll: returns as soon as there is something new for this person, or after `wait` seconds."""
        p = self.person(pid)
        floor = max(since, p.joined_event)           # never anything from before they joined
        end = time.monotonic() + max(0.0, min(wait, 25.0))
        while True:
            p.last_seen = time.time()
            fresh = [e for e in self.events if e["id"] > floor]
            if fresh or self.closed or time.monotonic() >= end:
                break
            try:
                async with self.cond:
                    await asyncio.wait_for(self.cond.wait(), timeout=min(5.0, max(0.05, end - time.monotonic())))
            except asyncio.TimeoutError:
                pass
        shown = [r for r in (self.render(e, p) for e in fresh) if r]
        return {"events": shown, "last": max([floor] + [e["id"] for e in fresh]), "closed": self.closed,
                **self.summary()}


ROOMS = {}


def create(max_size):
    if len(ROOMS) >= MAX_ROOMS:
        raise RoomError(503, "too many conversations are open right now")
    if not 2 <= max_size <= ROOM_CEILING:
        raise RoomError(400, f"the size must be between 2 and {ROOM_CEILING}")
    room = Room(max_size)
    ROOMS[room.id] = room
    return room


def get(rid):
    room = ROOMS.get(rid)
    if room is None:
        raise RoomError(404, "no such conversation (the link may have expired)")
    return room


async def sweep_forever():
    """Housekeeping once a minute: forget silent phones, drop finished and empty rooms."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for rid, room in list(ROOMS.items()):
            for pid, p in list(room.people.items()):
                if now - p.last_seen > FORGET_AFTER:
                    room.people.pop(pid, None)
                    await room.publish("leave", pid=pid, name=p.name, lang=p.lang)
            if room.people:
                room.empty_since = None
            elif room.empty_since is None:
                room.empty_since = now
            gone = now > room.expires + 300 or (room.closed and now - room.closed_at > 300)
            if gone or (room.empty_since and now - room.empty_since > 1800):
                ROOMS.pop(rid, None)
