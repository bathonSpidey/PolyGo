"""Web push: an invitation can reach a phone even when PolyGo is closed (it shows as a normal notification).

Each phone that turns notifications on sends us its push address (a "subscription"); we keep it, per device, in
push.json (private, git-ignored). The server signs every push with its own key pair (VAPID), made once and kept in
vapid_private.pem. A push carries only a short text and a link, never a conversation.
Only the big browser push services are accepted as addresses, so a phone cannot make this server call anything else.
"""
import asyncio, base64, json, os, tempfile, threading, time
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
CONTACT = os.environ.get("POLYGO_PUSH_CONTACT", "mailto:polygo@example.com")     # sent to the push service with each push
MAX_PER_DEVICE = 5
TTL = 2 * 3600                       # a push waits this long for a phone that is off or out of reach
ALLOWED_HOSTS = ("googleapis.com", "push.services.mozilla.com", "push.apple.com", "notify.windows.com")
_lock = threading.RLock()
_subs = {}                           # device id -> [{"endpoint", "keys": {"p256dh", "auth"}, "ts"}]
_loaded = False
_vapid = None


def sub_path():
    return os.environ.get("POLYGO_PUSH_FILE", os.path.join(HERE, "push.json"))


def key_path():
    return os.environ.get("POLYGO_VAPID_FILE", os.path.join(HERE, "vapid_private.pem"))


def _load():
    global _loaded, _subs
    if _loaded:
        return
    try:
        with open(sub_path(), encoding="utf-8") as f:
            data = json.load(f)
        _subs = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        _subs = {}
    _loaded = True


def _save():
    path = sub_path()
    fd, tmp = tempfile.mkstemp(prefix=".push-", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(_subs, f)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def reset_for_tests():
    global _loaded, _vapid
    with _lock:
        _loaded = False
        _vapid = None


# ---------- our own key pair ----------
def _vapid_key():
    global _vapid
    with _lock:
        if _vapid is None:
            from py_vapid import Vapid
            p = key_path()
            if os.path.exists(p):
                _vapid = Vapid.from_file(p)
            else:
                _vapid = Vapid()
                _vapid.generate_keys()
                _vapid.save_key(p)
                try:
                    os.chmod(p, 0o600)
                except OSError:
                    pass
        return _vapid


def public_key():
    """The server's public key as the browser wants it (URL-safe base64 of the raw point)."""
    from cryptography.hazmat.primitives import serialization
    raw = _vapid_key().public_key.public_bytes(serialization.Encoding.X962,
                                               serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# ---------- subscriptions ----------
def valid_endpoint(url):
    try:
        u = urlparse(str(url))
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return (u.scheme == "https" and len(str(url)) <= 1000 and not u.username and not u.password
            and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS))


def add(did, sub):
    """Remember a phone's push address. Returns False if it is not usable."""
    if not isinstance(sub, dict) or not valid_endpoint(sub.get("endpoint")):
        return False
    keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else {}
    p256, auth = keys.get("p256dh"), keys.get("auth")
    if not (isinstance(p256, str) and isinstance(auth, str) and 0 < len(p256) <= 200 and 0 < len(auth) <= 100):
        return False
    with _lock:
        _load()
        mine = [s for s in _subs.get(did, []) if s["endpoint"] != sub["endpoint"]]   # the same address replaces itself
        mine.append({"endpoint": sub["endpoint"], "keys": {"p256dh": p256, "auth": auth}, "ts": time.time()})
        _subs[did] = mine[-MAX_PER_DEVICE:]
        _save()
    return True


def remove(did, endpoint=None):
    with _lock:
        _load()
        if did not in _subs:
            return
        if endpoint:
            left = [s for s in _subs[did] if s["endpoint"] != endpoint]
            if left:
                _subs[did] = left
            else:
                del _subs[did]
        else:
            del _subs[did]
        _save()


def count(did):
    with _lock:
        _load()
        return len(_subs.get(did, []))


# ---------- sending ----------
def _send_one(sub, payload):
    from pywebpush import webpush
    webpush(subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]}, data=json.dumps(payload),
            vapid_private_key=_vapid_key(), vapid_claims={"sub": CONTACT}, ttl=TTL, timeout=10,
            headers={"Urgency": "high"})


async def send(did, payload):
    """Push a short message to every phone of this device. Never raises; returns how many were accepted."""
    with _lock:
        _load()
        targets = list(_subs.get(did, []))
    if not targets:
        return 0
    try:
        _vapid_key()
    except Exception:
        return 0
    sent = 0
    for sub in targets:
        try:
            await asyncio.to_thread(_send_one, sub, payload)
            sent += 1
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):                    # the phone cancelled or the address expired: forget it
                remove(did, sub["endpoint"])
    return sent
