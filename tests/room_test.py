"""End-to-end test of shared conversations against a running PolyGo server (uses the real translator).

Run:  python tests/room_test.py http://127.0.0.1:8765 <family passcode>
It creates and ends its own rooms; it needs the server and the Claude login to be working."""
import json, sys, time, urllib.request, urllib.error

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
PASS = sys.argv[2] if len(sys.argv) > 2 else sys.exit("usage: room_test.py <base url> <family passcode>")
results = []


def call(method, path, body=None, passcode=PASS, timeout=70):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if passcode is not None:
        req.add_header("X-Passcode", passcode)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def check(label, cond, extra=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + label + (("  -> " + str(extra)) if extra != "" else ""))


def poll(rid, who, wait=0):
    s, d = call("GET", f"/api/rooms/{rid}/events?pid={who['pid']}&since={who['since']}&wait={wait}")
    if s == 200:
        who["since"] = d["last"]
    return s, d


def messages(d):
    return [e for e in d.get("events", []) if e["kind"] == "message"]


print("== access ==")
s, _ = call("POST", "/api/rooms", {"name": "x", "lang": "German"}, passcode=None)
check("no passcode is refused", s == 401, s)
s, _ = call("POST", "/api/rooms", {"name": "x", "lang": "German"}, passcode="wrong")
check("wrong passcode is refused", s == 401, s)
s, _ = call("GET", "/api/rooms/does-not-exist")
check("unknown room is 404", s == 404, s)

print("== create, join, capacity ==")
s, ana = call("POST", "/api/rooms", {"name": "Ana", "lang": "Assamese", "tone": "formal", "max_size": 3})
check("room created with size 3", s == 200 and ana.get("max_size") == 3 and ana.get("room"), (s, ana.get("max_size")))
rid = ana["room"]
ana = {"pid": ana["pid"], "since": ana["since"]}
check("room link id is long and random", len(rid) >= 20, len(rid))
s, d = call("POST", f"/api/rooms/{rid}/join", {"name": "Ben", "lang": "German", "tone": "formal"})
ben = {"pid": d["pid"], "since": d["since"]}
check("German phone joins", s == 200, s)
s, d = call("POST", f"/api/rooms/{rid}/join", {"name": "Cleo", "lang": "English", "tone": "informal"})
cleo = {"pid": d["pid"], "since": d["since"]}
check("English phone joins (3 of 3)", s == 200 and len(d["people"]) == 3, (s, len(d.get("people", []))))
s, d = call("POST", f"/api/rooms/{rid}/join", {"name": "Dev", "lang": "Hindi", "tone": "formal"})
check("a fourth phone is refused when the room is full", s == 409, (s, d.get("detail")))
s, d = call("POST", f"/api/rooms/{rid}/size", {"pid": ben["pid"], "max_size": 5})
check("only the creator can change the size", s == 403, s)
s, d = call("POST", f"/api/rooms/{rid}/size", {"pid": ana["pid"], "max_size": 99})
check("size above the ceiling is refused", s == 400, (s, d.get("detail")))
s, d = call("POST", f"/api/rooms/{rid}/size", {"pid": ana["pid"], "max_size": 2})
check("size below the people present is refused", s == 400, (s, d.get("detail")))

print("== a message reaches everyone in their own language ==")
s, r = call("POST", f"/api/rooms/{rid}/messages", {"pid": ana["pid"], "text": "bhat khali?"})
check("Assamese message accepted, translated into German and English at once", s == 200 and r["ok"] and r["languages"] == ["English", "German"], (s, r))
_, d_ben = poll(rid, ben, 3)
_, d_cleo = poll(rid, cleo, 3)
_, d_ana = poll(rid, ana, 3)
mb, mc, ma = messages(d_ben), messages(d_cleo), messages(d_ana)
print("     Ben sees :", mb[0]["text"] if mb else None)
print("     Cleo sees:", mc[0]["text"] if mc else None)
check("German phone gets German", mb and "gegessen" in mb[0]["text"].lower(), mb[0]["text"] if mb else None)
check("English phone gets English", mc and "eaten" in mc[0]["text"].lower(), mc[0]["text"] if mc else None)
check("sender sees their own words, marked mine, not translated", ma and ma[0]["mine"] and ma[0]["text"] == "bhat khali?" and not ma[0]["translated"], ma[0] if ma else None)
check("original text travels with the translation", mb and mb[0]["original"] == "bhat khali?", "")

print("== resize, then a latecomer sees no history ==")
s, d = call("POST", f"/api/rooms/{rid}/size", {"pid": ana["pid"], "max_size": 4})
check("creator raises the size to 4", s == 200 and d["max_size"] == 4, (s, d.get("max_size")))
s, d = call("POST", f"/api/rooms/{rid}/join", {"name": "Dev", "lang": "Hindi", "tone": "formal"})
dev = {"pid": d["pid"], "since": d["since"]}
check("Hindi phone now joins", s == 200, (s, d.get("detail")))
_, d_dev = poll(rid, dev, 0)
check("the latecomer sees NOTHING from before they joined", d_dev["events"] == [], d_dev["events"])

print("== four languages at once ==")
t0 = time.time()
s, r = call("POST", f"/api/rooms/{rid}/messages", {"pid": ben["pid"], "text": "Das Essen ist fertig, kommt bitte."})
print("     one call, three languages: %.1fs" % (time.time() - t0))
check("German message goes to Assamese, English and Hindi in one call", s == 200 and r["ok"] and r["languages"] == ["Assamese", "English", "Hindi"], (s, r))
_, d_ana = poll(rid, ana, 3)
_, d_cleo = poll(rid, cleo, 3)
_, d_dev = poll(rid, dev, 3)
_, d_ben = poll(rid, ben, 3)
ma, mc, md, mb = messages(d_ana), messages(d_cleo), messages(d_dev), messages(d_ben)
print("     Ana  sees:", ma[0]["text"] if ma else None, "| script:", ma[0].get("n") if ma else None)
print("     Cleo sees:", mc[0]["text"] if mc else None)
print("     Dev  sees:", md[0]["text"] if md else None)
check("Assamese phone gets Latin Assamese plus the script version", ma and ma[0]["text"].isascii() and ma[0].get("n"), ma[0] if ma else None)
en = mc[0]["text"].lower() if mc else ""
check("English phone gets English", bool(mc) and en.isascii() and any(w in en for w in ("ready", "dinner", "food", "meal")), en)
check("Hindi phone gets Devanagari", md and any(0x0900 <= ord(c) <= 0x097F for c in md[0]["text"]), md[0]["text"] if md else None)
check("sender gets own words back", mb and mb[0]["mine"] and mb[0]["text"].startswith("Das Essen"), "")
check("the latecomer sees the new message but still nothing older", md and len(md) == 1, len(md))

print("== profile change, reconnect, leave ==")
s, d = call("POST", f"/api/rooms/{rid}/profile", {"pid": ben["pid"], "lang": "English"})
check("a person can switch their language", s == 200, s)
s, r = call("POST", f"/api/rooms/{rid}/messages", {"pid": ana["pid"], "text": "moi bhal ase"})
check("targets now follow the switch (Ben is English)", s == 200 and r["languages"] == ["English", "Hindi"], r.get("languages"))
_, d_ben = poll(rid, ben, 3)
mb = messages(d_ben)
check("Ben now receives English", mb and mb[-1]["text"].isascii(), mb[-1]["text"] if mb else None)
s, d = poll(rid, ben, 1)
check("a quiet poll returns nothing new (reconnect is safe)", s == 200 and d["events"] == [], d["events"])
s, d = call("POST", f"/api/rooms/{rid}/join", {"name": "Ben", "lang": "English", "tone": "formal", "pid": ben["pid"]})
check("reloading the page rejoins as the same person, even in a full room", s == 200 and d["pid"] == ben["pid"], (s, d.get("detail")))
s, d = call("POST", f"/api/rooms/{rid}/leave", {"pid": cleo["pid"]})
_, d_ana = poll(rid, ana, 2)
check("others are told when someone leaves", any(e["kind"] == "leave" and e["name"] == "Cleo" for e in d_ana["events"]), [e["kind"] for e in d_ana["events"]])

print("== ending ==")
s, d = call("POST", f"/api/rooms/{rid}/end", {"pid": ben["pid"]})
check("only the creator can end it", s == 403, s)
s, d = call("POST", f"/api/rooms/{rid}/end", {"pid": ana["pid"]})
check("the creator ends it", s == 200, s)
_, d_dev = poll(rid, dev, 2)
check("everyone is told it ended", d_dev["closed"] and any(e["kind"] == "end" for e in d_dev["events"]), [e["kind"] for e in d_dev["events"]])
s, d = call("POST", f"/api/rooms/{rid}/messages", {"pid": dev["pid"], "text": "hello"})
check("nobody can send into an ended room", s == 410, (s, d.get("detail")))
s, d = call("POST", f"/api/rooms/{rid}/join", {"name": "Late", "lang": "German"})
check("nobody can join an ended room", s == 410, (s, d.get("detail")))

print("== limits ==")
s, solo = call("POST", "/api/rooms", {"name": "Solo", "lang": "German"})
codes = []
for i in range(10):
    codes.append(call("POST", f"/api/rooms/{solo['room']}/messages", {"pid": solo["pid"], "text": f"test {i}"})[0])
check("a person can send 8 messages per 30 s, then is slowed", codes[:8] == [200] * 8 and 429 in codes[8:], codes)
s, d = call("POST", f"/api/rooms/{solo['room']}/messages", {"pid": solo["pid"], "text": "x" * 700})
check("an over-long message is refused", s in (400, 429), (s, d.get("detail")))
s, d = call("POST", "/api/rooms", {"name": "Tiny", "lang": "German", "max_size": 1})
check("a room needs at least 2 places", s == 400, (s, d.get("detail")))
s, d = call("POST", "/api/rooms", {"name": "X", "lang": "Klingon"})
check("unknown language is refused", s == 400, (s, d.get("detail")))

print()
print(f"{sum(results)} of {len(results)} checks passed")
sys.exit(0 if all(results) else 1)
