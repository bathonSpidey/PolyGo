"""Regression test: a phone that stops checking in (screen off, other window) must still get its translation.

Start a throwaway server whose 'present' window is short, then run this against it:
  POLYGO_ROOM_ACTIVE_SECONDS=3 uvicorn server:app --host 127.0.0.1 --port 8766
  python tests/asleep_test.py http://127.0.0.1:8766 <admin invite token>   (from: python devices.py recovery)
"""
import json, sys, time, urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else sys.exit("usage: asleep_test.py <base url> <admin invite token>")
TOKEN = sys.argv[2] if len(sys.argv) > 2 else sys.exit("usage: asleep_test.py <base url> <admin invite token>")


def device_key(token):
    """Redeem an admin invite token (python devices.py recovery) and return the device key from its cookie."""
    req = urllib.request.Request(BASE + "/api/invite/redeem", method="POST", data=json.dumps({"token": token}).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        cookie = r.headers["Set-Cookie"]
    return cookie.split(";")[0].split("=", 1)[1]

PASS = device_key(TOKEN)


def call(method, path, body=None, key=PASS):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode("utf-8") if body is not None else None)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Device", key)
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.loads(r.read())


# two different phones, as in real life: each person's name is their phone's name
call_inv = call("POST", "/api/admin/invites", {"name": "Fritz", "lang": "de"})
FRITZ = device_key(call_inv["token"])


r = call("POST", "/api/rooms", {"lang": "English", "max_size": 4})
rid, en = r["room"], {"pid": r["pid"], "since": r["since"]}
j = call("POST", f"/api/rooms/{rid}/join", {"lang": "German"}, key=FRITZ)
de = {"pid": j["pid"]}
assert j["name"] == "Fritz" and r["name"] != "Fritz", (r["name"], j["name"])
time.sleep(6)                                    # the English phone is silent for longer than the presence window
s = call("POST", f"/api/rooms/{rid}/messages", {"pid": de["pid"], "text": "Wie gehts"})
d = call("GET", f"/api/rooms/{rid}/events?pid={en['pid']}&since={en['since']}&wait=2")
msgs = [e for e in d["events"] if e["kind"] == "message"]
call("POST", f"/api/rooms/{rid}/end", {"pid": en["pid"]})
ok = s["languages"] == ["English"] and msgs and msgs[0]["translated"] and "how" in msgs[0]["text"].lower()
print("PASS: the silent English phone found its translation:" if ok else "FAIL:", msgs[0]["text"] if msgs else s)
sys.exit(0 if ok else 1)
