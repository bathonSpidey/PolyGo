"""Invitations to a conversation, against a running PolyGo server (no translator needed).

Run:  python tests/invite_test.py http://127.0.0.1:8765 <admin invite token>    (token from: python devices.py recovery)
It makes its own test phones and conversations."""
import json, sys, urllib.request, urllib.error

BASE = sys.argv[1] if len(sys.argv) > 1 else sys.exit("usage: invite_test.py <base url> <admin invite token>")
TOKEN = sys.argv[2] if len(sys.argv) > 2 else sys.exit("usage: invite_test.py <base url> <admin invite token>")
results = []


def check(label, cond, extra=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + label + (("  -> " + str(extra)) if extra != "" else ""))


def call(method, path, body=None, key=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("X-Device", key)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}"), r.headers
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}"), e.headers
        except Exception:
            return e.code, {}, e.headers


def redeem(token):
    s, d, h = call("POST", "/api/invite/redeem", {"token": token})
    assert s == 200, (s, d)
    return h["Set-Cookie"].split(";")[0].split("=", 1)[1], d["device"]["id"]


ADMIN, ADMIN_ID = redeem(TOKEN)


def phone(name, lang="en", **extra):
    s, d, _ = call("POST", "/api/admin/invites", {"name": name, "lang": lang, **extra}, ADMIN)
    assert s == 200, (s, d)
    return redeem(d["token"])


SMITA, SMITA_ID = phone("InvSmita", "en")
MECH, MECH_ID = phone("InvMechthild", "de")
FRIEND, FRIEND_ID = phone("InvFriend", "en", access_days=2)

# ---- the list of phones
s, d, _ = call("GET", "/api/people", key=ADMIN)
names = [p["name"] for p in d.get("people", [])]
check("the list has the other phones, by name", s == 200 and "InvSmita" in names and "InvMechthild" in names, names)
check("...but not this phone itself", all(p["id"] != ADMIN_ID for p in d["people"]))
check("...and nothing secret (only id, name, language)", all(set(p) == {"id", "name", "lang"} for p in d["people"]))
check("...sorted by name", names == sorted(names, key=str.lower), names)
s, d, _ = call("GET", "/api/people", key=FRIEND)
check("a friend's phone cannot browse the family", s == 200 and d["people"] == [] and d["allowed"] is False, d)
check("a phone that is not set up gets nothing", call("GET", "/api/people")[0] == 401)

# ---- a conversation and invitations
s, room, _ = call("POST", "/api/rooms", {"lang": "English", "max_size": 3}, ADMIN)
rid, pid = room["room"], room["pid"]
check("the admin starts a conversation", s == 200 and rid)
check("nobody is invited yet", room.get("invited") == [], room.get("invited"))
s, d, _ = call("GET", "/api/me/invitations", key=SMITA)
check("Smita has no invitations yet", s == 200 and d["invitations"] == [], d)

s, d, _ = call("POST", f"/api/rooms/{rid}/invite", {"pid": pid, "ids": [SMITA_ID, MECH_ID]}, ADMIN)
check("the admin invites Smita and Mechthild", s == 200 and sorted(d["asked"]) == ["InvMechthild", "InvSmita"], (s, d))
check("the room lists them as invited", sorted(d["invited"]) == ["InvMechthild", "InvSmita"], d.get("invited"))
s, d, _ = call("GET", "/api/me/invitations", key=SMITA)
inv = d["invitations"][0] if d.get("invitations") else {}
ADMIN_NAME = call("GET", "/api/me", key=ADMIN)[1]["device"]["name"]
check("Smita sees the invitation: who asked, the room, who is there", inv.get("room") == rid and inv.get("from") == ADMIN_NAME, inv)
check("...with the people already in it and no 'full' flag", inv.get("people") == [ADMIN_NAME] and inv.get("full") is False, inv)
check("Mechthild has hers too", len(call("GET", "/api/me/invitations", key=MECH)[1]["invitations"]) == 1)
check("the friend has none", call("GET", "/api/me/invitations", key=FRIEND)[1]["invitations"] == [])

# ---- the invitation event reaches the people inside
s, d, _ = call("GET", f"/api/rooms/{rid}/events?pid={pid}&since=0&wait=0", key=ADMIN)
kinds = [e["kind"] for e in d.get("events", [])]
check("the people inside see an 'invite' line (so they are woken at once)", "invite" in kinds, kinds)

# ---- joining removes the invitation
s, d, _ = call("POST", f"/api/rooms/{rid}/join", {"lang": "English", "tone": "formal"}, SMITA)
spid = d.get("pid")
check("Smita joins from the invitation", s == 200 and spid, (s, d))
check("...her invitation is gone", call("GET", "/api/me/invitations", key=SMITA)[1]["invitations"] == [])
check("...and the room no longer lists her as invited", d["invited"] == ["InvMechthild"], d.get("invited"))

# ---- anyone inside can add another person
s, d, _ = call("POST", f"/api/rooms/{rid}/invite", {"pid": spid, "ids": [FRIEND_ID]}, SMITA)
check("Smita (not the admin) can invite another phone", s == 200 and d["asked"] == ["InvFriend"], (s, d))
check("a phone that is already inside is not invited again", call("POST", f"/api/rooms/{rid}/invite", {"pid": spid, "ids": [SMITA_ID]}, SMITA)[0] == 404)
check("someone who is not in the conversation cannot invite", call("POST", f"/api/rooms/{rid}/invite", {"pid": "nope", "ids": [MECH_ID]}, MECH)[0] == 404)
check("a friend's phone cannot send invitations", call("POST", f"/api/rooms/{rid}/invite", {"pid": spid, "ids": [MECH_ID]}, FRIEND)[0] == 403)
check("unknown ids are ignored; nothing valid means a refusal", call("POST", f"/api/rooms/{rid}/invite", {"pid": spid, "ids": ["zzz"]}, SMITA)[0] == 404)
check("more than 10 at once is refused", call("POST", f"/api/rooms/{rid}/invite", {"pid": spid, "ids": [MECH_ID] * 3 + [str(i) for i in range(12)]}, SMITA)[0] == 400)

# ---- declining
s, d, _ = call("POST", "/api/me/invitations/decline", {"room": rid}, MECH)
check("Mechthild declines", s == 200)
check("...her invitation is gone and the room stops listing her", call("GET", "/api/me/invitations", key=MECH)[1]["invitations"] == [])
s, d, _ = call("GET", f"/api/rooms/{rid}", key=ADMIN)
check("...the room shows only the friend as still invited", d.get("invited") == ["InvFriend"], d.get("invited"))
check("declining a room that does not exist is harmless", call("POST", "/api/me/invitations/decline", {"room": "nope"}, MECH)[0] == 200)

# ---- a full room says so
call("POST", f"/api/rooms/{rid}/join", {"lang": "German", "tone": "formal"}, MECH)       # 3 of 3 now
call("POST", f"/api/rooms/{rid}/invite", {"pid": pid, "ids": [FRIEND_ID]}, ADMIN)
inv = call("GET", "/api/me/invitations", key=FRIEND)[1]["invitations"]
check("an invitation to a full room is flagged full", inv and inv[0]["full"] is True, inv)

# ---- an ended room offers nothing
call("POST", f"/api/rooms/{rid}/end", {"pid": pid}, ADMIN)
check("after the conversation ends, invitations vanish", call("GET", "/api/me/invitations", key=FRIEND)[1]["invitations"] == [])
check("and nobody can be invited to it", call("POST", f"/api/rooms/{rid}/invite", {"pid": pid, "ids": [FRIEND_ID]}, ADMIN)[0] in (404, 410))

# ---- tidy up the test phones
for did in (SMITA_ID, MECH_ID, FRIEND_ID):
    call("DELETE", f"/api/admin/devices/{did}", key=ADMIN)

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
