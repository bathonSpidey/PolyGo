"""Tests for devices and invites. Two parts:

  python tests/devices_test.py unit
      in-process checks of devices.py with a temporary file (no server needed)

  python tests/devices_test.py http <base url> <family passcode> <path of the server's devices file>
      against a running server that was started with an EMPTY devices file, e.g.
        POLYGO_DEVICES_FILE=/tmp/d.json POLYGO_PASSCODE_FILE=/tmp/p.txt uvicorn server:app --port 8766
"""
import json, os, subprocess, sys, tempfile, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
results = []


def check(label, cond, extra=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + label + (("  -> " + str(extra)) if extra != "" else ""))


def finish():
    bad = results.count(False)
    print(f"\n{len(results) - bad}/{len(results)} checks passed")
    sys.exit(1 if bad else 0)


# ------------------------------------------------------------------ unit
def unit():
    tmp = tempfile.mkdtemp()
    os.environ["POLYGO_DEVICES_FILE"] = os.path.join(tmp, "devices.json")
    import devices
    D = devices.DeviceError

    def raises(fn, status):
        try:
            fn()
        except D as e:
            return e.status == status
        return False

    check("no admin at the start", not devices.have_admin())
    a1, k1 = devices.claim_admin("  Abir   Phone ", "en")
    check("first claim gives an admin with a tidy name", a1["admin"] and a1["name"] == "Abir Phone")
    check("claim refused once an admin exists", raises(lambda: devices.claim_admin("X", "en"), 409))
    check("the key is only stored as a hash", k1 not in open(devices.path()).read())
    if os.name != "nt":
        check("devices file is private (600)", oct(os.stat(devices.path()).st_mode & 0o777) == "0o600")
    check("the key finds the device", devices.authenticate(k1)["id"] == a1["id"])
    check("a wrong or empty key finds nobody", devices.authenticate("nope") is None and devices.authenticate("") is None
          and devices.authenticate("x" * 500) is None)

    check("the last admin cannot be removed", raises(lambda: devices.forget(a1["id"], a1), 409))

    inv, tok = devices.create_invite(a1, "Smita", "en")
    check("family invite never expires, is not admin", inv["link_expires"] == 0 and not inv["admin"])
    check("partner defaults: English -> German", inv["partner"] == "German")
    check("token is not stored", tok not in open(devices.path()).read())
    check("duplicate name refused (even against open invites)", raises(lambda: devices.create_invite(a1, "smita", "en"), 409))
    d2, k2 = devices.redeem(tok)
    check("redeem makes a device with the invite's settings", d2["name"] == "Smita" and d2["lang"] == "English" and not d2["admin"])
    check("a link works only once", raises(lambda: devices.redeem(tok), 404))
    check("junk tokens are refused", raises(lambda: devices.redeem("junk"), 404) and raises(lambda: devices.redeem(""), 404))
    check("German phone talks with Assamese", devices.create_invite(a1, "Mechthild", "de")[0]["partner"] == "Assamese")
    sm = devices.set_lang(d2["id"], "as")
    check("a phone can change its own language (Assamese talks with German)", sm["lang"] == "Assamese" and sm["partner"] == "German")
    check("...also by code, and German then talks with Assamese", devices.set_lang(d2["id"], "de")["partner"] == "Assamese")
    check("...a bad language or an unknown phone is refused", raises(lambda: devices.set_lang(d2["id"], "xx"), 400) and raises(lambda: devices.set_lang("nobody", "en"), 404))
    check("...and the change survives a reload from disk", (devices.reset_for_tests(), devices.authenticate(k2)["lang"])[1] == "German")
    devices.set_lang(d2["id"], "en")
    pr = devices.set_lang(d2["id"], "de", "hi")
    check("a phone can pick both its languages (German with Hindi)", pr["lang"] == "German" and pr["partner"] == "Hindi")
    check("...but not the same one twice, or an unknown one", raises(lambda: devices.set_lang(d2["id"], "de", "German"), 400) and raises(lambda: devices.set_lang(d2["id"], "de", "xx"), 400))
    check("...and a refused change leaves the pair as it was", devices.authenticate(k2)["partner"] == "Hindi")
    devices.set_lang(d2["id"], "en")

    inv, tok = devices.create_invite(a1, "Fritz", "de", link_hours=0.0002)
    time.sleep(1.2)
    check("expired link refused", raises(lambda: devices.redeem(tok), 410))

    inv, tok = devices.create_invite(a1, "Friend", "de", access_days=2)
    d3, k3 = devices.redeem(tok)
    check("friend access has an end date about 2 days away", 1.9 * 86400 < d3["expires_at"] - time.time() < 2.1 * 86400)
    devices._state["devices"][d3["id"]]["expires_at"] = time.time() - 5
    check("a device past its access period is refused", devices.authenticate(k3) is None)

    inv, tok = devices.create_invite(a1, "Admin2", "en", admin=True)
    check("admin invite always expires (default 24 h)", 23 * 3600 < inv["link_expires"] - time.time() < 25 * 3600)
    inv_b, _ = devices.create_invite(a1, "Admin3", "en", admin=True, link_hours=9999)
    check("admin invite lifetime is capped at 7 days", inv_b["link_expires"] - time.time() <= 168 * 3600 + 5)
    a2, k2b = devices.redeem(tok)
    check("an admin invite makes an admin", a2["admin"] and devices.admin_count() == 2)
    check("with two admins one can be removed", devices.forget(a1["id"], a2) == "device")
    check("then the remaining one is protected", raises(lambda: devices.forget(a2["id"], a2), 409))
    check("a removed device's key stops working", devices.authenticate(k1) is None)
    check("an open invite can be cancelled", devices.forget(inv_b["id"], a2) == "invite")
    check("removing something unknown is a 404", raises(lambda: devices.forget("nothing", a2), 404))
    check("bad input refused", raises(lambda: devices.create_invite(a2, "", "en"), 400)
          and raises(lambda: devices.create_invite(a2, "Z", "xx"), 400)
          and raises(lambda: devices.create_invite(a2, "Z", "en", tone="rude"), 400)
          and raises(lambda: devices.create_invite(a2, "Z", "en", access_days=-1), 400))
    check("listing hides hashes", "key_hash" not in json.dumps(devices.listing()) and "token_hash" not in json.dumps(devices.listing()))

    # the recovery command runs in ANOTHER process and the server must notice the new invite
    out = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "devices.py"), "recovery", "Rescue", "1"],
                         capture_output=True, text=True, env=os.environ)
    tok = out.stdout.strip().split("#i=")[-1]
    check("recovery command prints a link", out.returncode == 0 and len(tok) > 15, out.stderr.strip()[:80])
    d4, _ = devices.redeem(tok)
    check("the recovery link makes an admin, seen without a restart", d4["admin"] and d4["name"] == "Rescue")
    finish()


# ------------------------------------------------------------------ http
def http():
    BASE, PASS, FILE = sys.argv[2], sys.argv[3], sys.argv[4]
    import devices  # for the name of the cookie only

    class Phone:
        def __init__(self):
            self.cookie = ""
            self.set_cookie_header = ""

        def call(self, method, path, body=None, passcode=None):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(BASE + path, data=data, method=method)
            req.add_header("Content-Type", "application/json")
            if passcode:
                req.add_header("X-Passcode", passcode)
            if self.cookie:
                req.add_header("Cookie", self.cookie)
            try:
                r = urllib.request.urlopen(req, timeout=30)
            except urllib.error.HTTPError as e:
                r = e
            sc = r.headers.get("Set-Cookie")
            if sc and sc.startswith(devices.COOKIE + "="):
                self.set_cookie_header = sc
                self.cookie = sc.split(";")[0]
            try:
                return r.status if hasattr(r, "status") else r.code, json.loads(r.read() or b"{}")
            except ValueError:
                return r.code, {}

    admin, smita, stranger, late = Phone(), Phone(), Phone(), Phone()

    s, d = stranger.call("GET", "/api/me")
    check("a new phone is not enrolled and may claim", s == 200 and d == {"enrolled": False, "can_claim": True}, d)
    t0 = time.time()
    s, d = stranger.call("POST", "/api/claim", {"passcode": "wrong", "name": "Evil", "lang": "en"})
    check("claim with a wrong passcode is refused (and slowed)", s == 401 and time.time() - t0 >= 0.9, s)
    check("...and nothing was created", stranger.call("GET", "/api/me")[1].get("can_claim") is True)
    s, d = admin.call("POST", "/api/claim", {"passcode": PASS, "name": "AbirPhone", "lang": "en"})
    check("claim with the passcode makes the first admin", s == 200 and d["device"]["admin"] and d["device"]["name"] == "AbirPhone", (s, d))
    h = admin.set_cookie_header
    check("the key cookie is HttpOnly, Secure and SameSite=Strict", "HttpOnly" in h and "Secure" in h and "samesite=strict" in h.lower(), h[:40] + "...")
    s, d = admin.call("GET", "/api/me")
    check("/api/me knows the admin by cookie alone", s == 200 and d["enrolled"] and d["device"]["admin"])
    check("no key or hash is ever returned", "key" not in json.dumps(d))
    s, d = admin.call("GET", "/api/health")
    check("a set-up phone gets the health detail", s == 200 and "model" in d and "turns" in d, d)
    s, d = stranger.call("POST", "/api/claim", {"passcode": PASS, "name": "Second", "lang": "en"})
    check("a second claim is refused even with the right passcode", s == 409, s)
    check("/api/me no longer offers a claim", stranger.call("GET", "/api/me")[1] == {"enrolled": False, "can_claim": False})

    s, d = admin.call("GET", "/api/admin/devices")
    me = d.get("me")
    check("admin sees the list, 1 admin", s == 200 and d["admins"] == 1 and len(d["devices"]) == 1)
    s, d = admin.call("DELETE", f"/api/admin/devices/{me}")
    check("the only admin cannot remove itself", s == 409, d)
    s, d = admin.call("GET", "/api/admin/stats")
    check("the admin sees the stats (counters, 14 days, engine, Pi)", s == 200 and len(d["days"]) == 14 and "engine" in d and "pi" in d and len(d["devices"]) == 1 and "totals" in d, s)

    s, d = admin.call("POST", "/api/admin/invites", {"name": "Smita", "lang": "en"})
    tok = d.get("token", "")
    check("admin makes a family invite that never expires", s == 200 and tok and d["invite"]["link_expires"] == 0, (s, d))
    check("the token is not in the devices file", tok and tok not in open(FILE).read())
    s, d = admin.call("POST", "/api/admin/invites", {"name": "smita", "lang": "en"})
    check("duplicate name refused", s == 409, d)
    s, d = stranger.call("POST", "/api/admin/invites", {"name": "Hack", "lang": "en"})
    check("a phone without a key cannot invite", s == 401, s)

    s, d = smita.call("POST", "/api/invite/redeem", {"token": tok})
    check("opening the link enrols Smita (English, partner German, not admin)",
          s == 200 and d["device"]["name"] == "Smita" and d["device"]["lang"] == "English" and d["device"]["partner"] == "German" and not d["device"]["admin"], (s, d))
    s, d = smita.call("GET", "/api/me")
    check("afterwards the plain address recognises her", d.get("enrolled") and d["device"]["name"] == "Smita")
    t0 = time.time()
    s, d = late.call("POST", "/api/invite/redeem", {"token": tok})
    check("the same link on a second phone is refused", s == 404 and not late.cookie, s)
    check("...and Smita still works", smita.call("GET", "/api/me")[1].get("enrolled"))
    s, d = smita.call("GET", "/api/admin/devices")
    check("Smita is not an admin", s == 403, s)
    s, d = smita.call("POST", "/api/admin/invites", {"name": "Hack", "lang": "en"})
    check("Smita cannot invite", s == 403, s)
    check("Smita cannot see the stats", smita.call("GET", "/api/admin/stats")[0] == 403)
    check("a phone without a key cannot see the stats", stranger.call("GET", "/api/admin/stats")[0] == 401)
    s, d = smita.call("POST", "/api/me/profile", {"lang": "as"})
    check("a phone changes its own language (page and speech)", s == 200 and d["device"]["lang"] == "Assamese" and d["device"]["partner"] == "German", (s, d))
    check("...and the plain address now says so", smita.call("GET", "/api/me")[1]["device"]["lang"] == "Assamese")
    s, d = smita.call("POST", "/api/me/profile", {"lang": "German", "partner": "Hindi"})
    check("a phone picks both buttons' languages", s == 200 and d["device"]["lang"] == "German" and d["device"]["partner"] == "Hindi", (s, d))
    s, d = smita.call("POST", "/api/me/profile", {"lang": "German", "partner": "German"})
    check("the same language on both buttons is refused", s == 400, s)
    smita.call("POST", "/api/me/profile", {"lang": "as"})
    s, d = smita.call("POST", "/api/me/profile", {"lang": "Klingon"})
    check("an unknown language is refused", s == 400, s)
    s, d = stranger.call("POST", "/api/me/profile", {"lang": "de"})
    check("a phone without a key cannot change anything", s == 401, s)
    s, d = smita.call("POST", "/api/me/profile", {"lang": "en"})
    check("...and she can switch back", s == 200 and d["device"]["lang"] == "English", s)

    s, d = smita.call("POST", "/api/rooms", {"lang": "English", "max_size": 4})
    check("an enrolled phone can use the app without any passcode", s == 200 and d.get("room"), (s, str(d)[:60]))
    s, d = stranger.call("POST", "/api/rooms", {"lang": "English", "max_size": 4})
    check("a phone with neither key nor passcode cannot", s == 401, s)
    s, d = stranger.call("POST", "/api/rooms", {"lang": "English", "max_size": 4}, passcode=PASS)
    check("the family passcode opens nothing any more", s == 401, s)
    s, d = stranger.call("POST", "/api/translate", {"a": "German", "b": "Assamese", "text": "Hallo"}, passcode=PASS)
    check("...not even the translator", s == 401, s)
    s, d = stranger.call("GET", "/api/health", passcode=PASS)
    check("...and the health check shows no detail for it", s == 200 and d == {"ok": True}, d)

    bad = Phone(); bad.cookie = devices.COOKIE + "=" + "A" * 60
    check("a forged key is refused", bad.call("GET", "/api/me")[1].get("enrolled") is False and bad.call("POST", "/api/rooms", {"lang": "English", "max_size": 4})[0] == 401)
    bad.cookie = devices.COOKIE + "=" + "B" * 5000
    check("a huge cookie is refused, not a crash", bad.call("GET", "/api/me")[0] == 200)
    check("junk tokens give 404, not 500", late.call("POST", "/api/invite/redeem", {"token": "x" * 5000})[0] == 404)

    s, d = admin.call("POST", "/api/admin/invites", {"name": "Quick", "lang": "de", "link_hours": 0.0003})
    quick = d.get("token", "")
    time.sleep(1.5)
    s, d = late.call("POST", "/api/invite/redeem", {"token": quick})
    check("an expired link is refused", s == 410, (s, d))

    s, d = admin.call("POST", "/api/admin/invites", {"name": "Friend", "lang": "de", "access_days": 1})
    friend = Phone()
    s, d = friend.call("POST", "/api/invite/redeem", {"token": d["token"]})
    age = int(friend.set_cookie_header.split("Max-Age=")[1].split(";")[0])
    check("a friend's key lasts one day (cookie Max-Age)", s == 200 and 86000 < age <= 86400, age)

    s, d = admin.call("POST", "/api/admin/invites", {"name": "AbirPhone 2", "lang": "en", "admin": True})
    check("admin invite expires on its own", s == 200 and d["invite"]["link_expires"] > 0)
    admin2 = Phone()
    s, d = admin2.call("POST", "/api/invite/redeem", {"token": d["token"]})
    check("a second admin is enrolled", s == 200 and d["device"]["admin"])
    s, d = admin.call("GET", "/api/admin/devices")
    check("two admins now", d["admins"] == 2)
    smita_id = [x["id"] for x in d["devices"] if x["name"] == "Smita"][0]
    s, d = admin.call("DELETE", f"/api/admin/devices/{smita_id}")
    check("admin removes Smita's device", s == 200 and d["removed"] == "device", d)
    check("her key stops working at once", smita.call("GET", "/api/me")[1].get("enrolled") is False and smita.call("POST", "/api/rooms", {"lang": "English", "max_size": 4})[0] == 401)
    s, d = admin2.call("DELETE", f"/api/admin/devices/{me}")
    check("one admin may remove the other while two exist", s == 200, d)
    check("the removed admin is locked out", admin.call("GET", "/api/admin/devices")[0] == 401)
    s, d = admin2.call("GET", "/api/admin/devices")
    check("the last admin is protected again", admin2.call("DELETE", f"/api/admin/devices/{d['me']}")[0] == 409)
    s, d = admin2.call("POST", "/api/logout")
    check("logout clears the cookie", s == 200 and "Max-Age=0" in admin2.set_cookie_header)
    finish()


if len(sys.argv) > 1 and sys.argv[1] == "unit":
    unit()
elif len(sys.argv) > 4 and sys.argv[1] == "http":
    http()
else:
    sys.exit(__doc__)
