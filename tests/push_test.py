"""Unit tests for web push storage and sending (no network: the sender is replaced):  python tests/push_test.py"""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TMP = tempfile.mkdtemp()
os.environ["POLYGO_PUSH_FILE"] = os.path.join(TMP, "push.json")
os.environ["POLYGO_VAPID_FILE"] = os.path.join(TMP, "vapid.pem")
import push

results = []


def check(label, cond, extra=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + label + (("  -> " + str(extra)) if extra != "" else ""))


GOOD = "https://fcm.googleapis.com/fcm/send/abc"
keys = {"p256dh": "B" + "A" * 80, "auth": "a" * 22}
check("push services we know are accepted", all(push.valid_endpoint(u) for u in (
    GOOD, "https://updates.push.services.mozilla.com/wpush/v2/x", "https://web.push.apple.com/x",
    "https://wns2.notify.windows.com/x")))
check("anything else is refused", not any(push.valid_endpoint(u) for u in (
    "http://fcm.googleapis.com/x", "https://evil.example.com/x", "https://googleapis.com.evil.com/x",
    "https://127.0.0.1/x", "https://user:pw@fcm.googleapis.com/x", "", None, "ftp://fcm.googleapis.com/x",
    "https://fcm.googleapis.com/" + "x" * 1100)))

check("a good address is stored", push.add("d1", {"endpoint": GOOD, "keys": keys}) and push.count("d1") == 1)
check("the same address twice is one", push.add("d1", {"endpoint": GOOD, "keys": keys}) and push.count("d1") == 1)
check("bad input is refused", not push.add("d1", {"endpoint": GOOD, "keys": {}}) and not push.add("d1", "x")
      and not push.add("d1", {"endpoint": "https://x.test/", "keys": keys}))
for i in range(8):
    push.add("d2", {"endpoint": GOOD + str(i), "keys": keys})
check("at most 5 addresses per phone (the oldest go)", push.count("d2") == 5)
push.reset_for_tests()
check("addresses survive a restart", push.count("d1") == 1 and push.count("d2") == 5)

# ---- our own key
k1 = push.public_key()
check("the public key looks right (65 bytes, URL-safe base64)",
      len(k1) == 87 and "=" not in k1 and "+" not in k1 and "/" not in k1, k1)
check("the key file was made", os.path.exists(push.key_path()))
push.reset_for_tests()
check("the same key is used after a restart", push.public_key() == k1)

# ---- sending
sent = []


class Gone(Exception):
    def __init__(self, code):
        self.response = type("R", (), {"status_code": code})()


def fake(sub, payload):
    sent.append((sub["endpoint"], payload))
    if sub["endpoint"].endswith("dead"):
        raise Gone(410)
    if sub["endpoint"].endswith("odd"):
        raise Gone(500)
    if sub["endpoint"].endswith("boom"):
        raise RuntimeError("network down")


push._send_one = fake
run = asyncio.run
push.add("d3", {"endpoint": GOOD + "ok", "keys": keys})
push.add("d3", {"endpoint": GOOD + "dead", "keys": keys})
push.add("d3", {"endpoint": GOOD + "odd", "keys": keys})
push.add("d3", {"endpoint": GOOD + "boom", "keys": keys})
n = run(push.send("d3", {"title": "T"}))
check("one working address out of four = one delivered", n == 1, n)
check("every address was tried", len(sent) == 4)
check("an expired address (410) is forgotten", push.count("d3") == 3)
check("a temporary failure (500) or network error keeps the address", push.count("d3") == 3)
check("nobody subscribed = nothing sent, no error", run(push.send("nobody", {"title": "T"})) == 0)
push.remove("d3", GOOD + "ok")
check("one address can be removed", push.count("d3") == 2)
push.remove("d3")
check("all of a phone's addresses can be removed", push.count("d3") == 0)

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
