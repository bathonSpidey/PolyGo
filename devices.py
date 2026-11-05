"""Devices and invites: who may use PolyGo.

Every phone that uses PolyGo is a *device* with its own random key (kept in a cookie on that phone). The server
stores only a hash of the key, so a leaked devices.json cannot be used to get in. A device is created in one of two ways:

  * the very first admin claims the server with the family passcode, but only while no admin exists yet, or
  * an admin makes an *invite*: a single-use random token that is turned into a device when the link is opened.

Nobody can forge a link: a token is 128 random bits and only its hash is stored. A link works once.
Admins can list and remove devices, and the last admin can never be removed (the SSH command at the bottom of
this file prints a fresh admin link if every admin phone is lost).

State is a small JSON file (devices.json, git-ignored, mode 600), written atomically and rarely.
"""
import hashlib, json, os, re, secrets, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE = "polygo_device"
LANGS = {"as": "Assamese", "de": "German", "en": "English", "hi": "Hindi"}
PARTNER = {"Assamese": "German", "German": "Assamese", "English": "German", "Hindi": "German"}   # who a one-to-one view talks with
TONES = ("formal", "informal", "auto")
ADMIN_LINK_DEFAULT_HOURS = 24      # an admin invite is powerful, so it always expires
ADMIN_LINK_MAX_HOURS = 168
MAX_DEVICES = 60
MAX_INVITES = 60
SAVE_EVERY = 300                   # seconds: "last used" is only written to disk this often (SD cards wear out)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_lock = threading.RLock()
_state = {"devices": {}, "invites": {}}
_index = {}                        # key hash -> device id
_stamp = None


class DeviceError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


def path():
    return os.environ.get("POLYGO_DEVICES_FILE", os.path.join(HERE, "devices.json"))


def _hash(secret):
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def clean_name(value):
    return re.sub(r"\s+", " ", _CONTROL.sub(" ", str(value or ""))).strip()[:30]


def lang_name(value):
    """Accepts 'de' or 'German' (any case); returns the full language name or None."""
    v = str(value or "").strip()
    if v.lower() in LANGS:
        return LANGS[v.lower()]
    for name in LANGS.values():
        if v.lower() == name.lower():
            return name
    return None


# ---------- storage ----------
def _stat():
    try:
        st = os.stat(path())
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _load():
    """Re-read the file if it changed on disk (the SSH recovery command writes to it from another process)."""
    global _state, _index, _stamp
    stamp = _stat()
    if stamp == _stamp and _stamp is not None:
        return
    try:
        with open(path(), encoding="utf-8") as f:
            data = json.load(f)
        _state = {"devices": data.get("devices", {}), "invites": data.get("invites", {})}
    except (OSError, ValueError):
        _state = {"devices": {}, "invites": {}}
    _index = {d["key_hash"]: i for i, d in _state["devices"].items()}
    _stamp = stamp


def _save():
    global _stamp
    p = path()
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p) or ".", prefix=".devices-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _stamp = _stat()


def reset_for_tests():
    global _stamp
    with _lock:
        _stamp = None


# ---------- views ----------
def public(d):
    return {k: d[k] for k in ("id", "name", "lang", "partner", "tone", "admin", "created", "first_used", "last_used",
                              "expires_at") if k in d}


def public_invite(i):
    return {k: i[k] for k in ("id", "name", "lang", "partner", "tone", "admin", "created", "link_expires",
                              "access_days", "by") if k in i}


def admin_count():
    with _lock:
        _load()
        return sum(1 for d in _state["devices"].values() if d.get("admin"))


def have_admin():
    return admin_count() > 0


# ---------- creating devices ----------
def _name_taken(name):
    low = name.lower()
    return any(d["name"].lower() == low for d in _state["devices"].values()) or \
        any(i["name"].lower() == low for i in _state["invites"].values())


def _new_device(name, lang, tone, admin, access_days=0):
    key = secrets.token_urlsafe(32)
    now = time.time()
    d = {"id": secrets.token_urlsafe(6), "name": name, "lang": lang, "partner": PARTNER[lang], "tone": tone,
         "admin": bool(admin), "key_hash": _hash(key), "created": now, "first_used": now, "last_used": now,
         "expires_at": now + access_days * 86400 if access_days else 0, "access_days": access_days}
    _state["devices"][d["id"]] = d
    _index[d["key_hash"]] = d["id"]
    return d, key


def claim_admin(name, lang):
    """The very first admin. The caller has already checked the family passcode. Only works while no admin exists."""
    with _lock:
        _load()
        if any(d.get("admin") for d in _state["devices"].values()):
            raise DeviceError(409, "this server already has an admin")
        name, lang = clean_name(name), lang_name(lang)
        if not name or not lang:
            raise DeviceError(400, "a name and a language are needed")
        d, key = _new_device(name, lang, "formal", True)
        _save()
        return d, key


def create_invite(by, name, lang, tone="formal", admin=False, access_days=0, link_hours=0):
    with _lock:
        _load()
        name, lang_full = clean_name(name), lang_name(lang)
        if not name:
            raise DeviceError(400, "a name is needed")
        if not lang_full:
            raise DeviceError(400, "unknown language")
        if tone not in TONES:
            raise DeviceError(400, "unknown tone")
        if not isinstance(access_days, int) or not 0 <= access_days <= 3650:
            raise DeviceError(400, "access days must be 0 (no limit) to 3650")
        link_hours = float(link_hours or 0)
        if admin:
            link_hours = min(link_hours or ADMIN_LINK_DEFAULT_HOURS, ADMIN_LINK_MAX_HOURS)
        elif not 0 <= link_hours <= 24 * 365:
            raise DeviceError(400, "bad link lifetime")
        if _name_taken(name):
            raise DeviceError(409, "that name is already used by a device or an open invite")
        if len(_state["invites"]) >= MAX_INVITES or len(_state["devices"]) >= MAX_DEVICES:
            raise DeviceError(503, "too many devices or open invites; remove some first")
        token = secrets.token_urlsafe(16)
        now = time.time()
        inv = {"id": secrets.token_urlsafe(6), "token_hash": _hash(token), "name": name, "lang": lang_full,
               "partner": PARTNER[lang_full], "tone": tone, "admin": bool(admin), "created": now,
               "link_expires": now + link_hours * 3600 if link_hours else 0, "access_days": access_days,
               "by": by.get("name", "") if by else "recovery"}
        _state["invites"][inv["id"]] = inv
        _save()
        return inv, token


def redeem(token):
    """Opening an invite link: the invite is used up and the phone gets its own device key."""
    with _lock:
        _load()
        h = _hash(str(token or ""))
        now = time.time()
        for iid, inv in list(_state["invites"].items()):
            if secrets.compare_digest(inv["token_hash"], h):
                if inv["link_expires"] and now > inv["link_expires"]:
                    del _state["invites"][iid]
                    _save()
                    raise DeviceError(410, "this link has expired")
                del _state["invites"][iid]                     # single use: gone before anything else can use it
                d, key = _new_device(inv["name"], inv["lang"], inv["tone"], inv["admin"], inv["access_days"])
                _save()
                return d, key
        raise DeviceError(404, "this link is not valid, or it was already used")


# ---------- using devices ----------
def authenticate(key):
    """The device that owns this key, or None (unknown, removed or past its access period)."""
    if not key or len(key) > 200:
        return None
    with _lock:
        _load()
        did = _index.get(_hash(key))
        d = _state["devices"].get(did) if did else None
        if d is None:
            return None
        now = time.time()
        if d.get("expires_at") and now > d["expires_at"]:
            return None
        if now - d.get("last_used", 0) > SAVE_EVERY:
            d["last_used"] = now
            _save()
        return d


def set_lang(did, lang, partner=None):
    """The language this phone speaks, and who it talks with: the usual partner, or the one it picked for its second button."""
    with _lock:
        _load()
        d = _state["devices"].get(did)
        if d is None:
            raise DeviceError(404, "this phone is not set up")
        name = lang_name(lang)
        if not name:
            raise DeviceError(400, "unknown language")
        other = PARTNER[name]
        if partner:
            other = lang_name(partner)
            if not other or other == name:
                raise DeviceError(400, "unknown language" if not other else "pick two different languages")
        d["lang"], d["partner"] = name, other
        _save()
        return d


def forget(did, by):
    """Remove a device, or cancel an open invite with this id. The last admin can never be removed."""
    with _lock:
        _load()
        d = _state["devices"].get(did)
        if d:
            if d.get("admin") and sum(1 for x in _state["devices"].values() if x.get("admin")) <= 1:
                raise DeviceError(409, "this is the only admin; make a second admin first")
            del _state["devices"][did]
            _index.pop(d["key_hash"], None)
            _save()
            return "device"
        if did in _state["invites"]:
            del _state["invites"][did]
            _save()
            return "invite"
        raise DeviceError(404, "no such device or invite")


def listing():
    with _lock:
        _load()
        now = time.time()
        return {"devices": [public(d) for d in sorted(_state["devices"].values(), key=lambda x: x["created"])],
                "invites": [public_invite(i) for i in sorted(_state["invites"].values(), key=lambda x: x["created"])
                            if not i["link_expires"] or i["link_expires"] > now],
                "admins": sum(1 for d in _state["devices"].values() if d.get("admin"))}


# ---------- recovery from the Pi's own shell: python devices.py recovery [name] [hours] ----------
if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "recovery":
        who = sys.argv[2] if len(sys.argv) > 2 else "Admin"
        hours = float(sys.argv[3]) if len(sys.argv) > 3 else ADMIN_LINK_DEFAULT_HOURS
        try:
            inv, tok = create_invite(None, who, "en", admin=True, link_hours=hours)
        except DeviceError as e:
            sys.exit(e.detail)
        print(f"Admin link valid for {hours:g} hours (works once):\n  <your PolyGo address>/#i={tok}")
    else:
        print("usage: python devices.py recovery [name] [hours]")
