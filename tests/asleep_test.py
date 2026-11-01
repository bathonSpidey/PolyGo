"""Regression test: a phone that stops checking in (screen off, other window) must still get its translation.

Start a throwaway server whose 'present' window is short, then run this against it:
  POLYGO_ROOM_ACTIVE_SECONDS=3 uvicorn server:app --host 127.0.0.1 --port 8766
  python tests/asleep_test.py http://127.0.0.1:8766 <family passcode>
"""
import json, sys, time, urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else sys.exit("usage: asleep_test.py <base url> <family passcode>")
PASS = sys.argv[2] if len(sys.argv) > 2 else sys.exit("usage: asleep_test.py <base url> <family passcode>")


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode("utf-8") if body is not None else None)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Passcode", PASS)
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.loads(r.read())


r = call("POST", "/api/rooms", {"name": "", "lang": "English", "max_size": 4})
rid, en = r["room"], {"pid": r["pid"], "since": r["since"]}
j = call("POST", f"/api/rooms/{rid}/join", {"name": "", "lang": "German"})
de = {"pid": j["pid"]}
assert (r["name"], j["name"]) == ("Guest 1", "Guest 2"), (r["name"], j["name"])
time.sleep(6)                                    # the English phone is silent for longer than the presence window
s = call("POST", f"/api/rooms/{rid}/messages", {"pid": de["pid"], "text": "Wie gehts"})
d = call("GET", f"/api/rooms/{rid}/events?pid={en['pid']}&since={en['since']}&wait=2")
msgs = [e for e in d["events"] if e["kind"] == "message"]
call("POST", f"/api/rooms/{rid}/end", {"pid": en["pid"]})
ok = s["languages"] == ["English"] and msgs and msgs[0]["translated"] and "how" in msgs[0]["text"].lower()
print("PASS: the silent English phone found its translation:" if ok else "FAIL:", msgs[0]["text"] if msgs else s)
sys.exit(0 if ok else 1)
