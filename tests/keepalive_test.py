"""Unit tests for the login keep-alive (no server and no real Claude call needed):  python tests/keepalive_test.py"""
import asyncio, json, os, subprocess, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TMP = tempfile.mkdtemp()
os.environ["POLYGO_KEEPALIVE_FILE"] = os.path.join(TMP, "keepalive.json")
import keepalive as k

results = []


def check(label, cond, extra=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + label + (("  -> " + str(extra)) if extra != "" else ""))


run = lambda coro: asyncio.run(coro)
calls = []


def fake(ok, err=""):
    def f():
        calls.append(1)
        return ok, err
    return f


H = 3600
now = time.time()
k.load()
check("a fresh start has never had a successful call, so a check is due", k.state["last_ok"] == 0 and k.due())
k.used_ok(now)
check("right after a successful call nothing is due", not k.due(now + 1))
check("23 hours later still nothing is due", not k.due(now + 23 * H))
check("25 hours later a check is due", k.due(now + 25 * H))

calls.clear()
check("not due: no call is made", run(k.check_once(fake(True), now + H)) == "skipped" and calls == [])

k.state["last_ok"] = now - 30 * H
check("due and it works: ok, and one call only", run(k.check_once(fake(True))) == "ok" and len(calls) == 1)
check("success renews last_ok and leaves no error", time.time() - k.state["last_ok"] < 5 and k.state["error"] == "" and not k.state["login_problem"])

k.state["last_ok"] = old = now - 30 * H
check("due and it fails with a login message: failed", run(k.check_once(fake(False, "Invalid authentication credentials (401)"))) == "failed")
check("a login failure is flagged as a login problem", k.state["login_problem"] and "401" in k.state["error"])
check("a failure does not pretend the login is fine", k.state["last_ok"] == old)
check("a failed check is tried again at the next look (still due)", k.due())

k.state["last_ok"] = old
run(k.check_once(fake(False, "the check timed out")))
check("a timeout is an error but not a login problem", k.state["error"] and not k.state["login_problem"])

k.used_ok()
check("a real translation working clears the error", k.state["error"] == "" and not k.state["login_problem"])

# state on disk
k.state["last_ok"] = old
run(k.check_once(fake(False, "Please run /login")))
k.save()
saved = json.load(open(k.path(), encoding="utf-8"))
check("the file holds only numbers and the short error", set(saved) == {"last_ok", "last_try", "error", "login_problem"})
if os.name == "posix":
    check("the file is private (mode 600)", oct(os.stat(k.path()).st_mode & 0o777) == "0o600")
k.state.update({"last_ok": 0.0, "error": "", "login_problem": False})
k.load()
check("it loads back what was saved", k.state["last_ok"] == old and k.state["login_problem"] and "login" in k.state["error"])
open(k.path(), "w").write("not json")
k.load()
check("a damaged file is ignored, not fatal", True)
snap = k.snapshot()
check("the snapshot has what the panel needs", {"last_ok", "last_try", "error", "login_problem", "idle_hours"} <= set(snap))

# the real probe function, with the process call faked
real = subprocess.run


def with_run(result=None, exc=None):
    def f(cmd, **kw):
        f.cmd, f.kw = cmd, kw
        if exc:
            raise exc
        return result
    return f


def cp(code, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


fr = with_run(cp(0, json.dumps({"is_error": False, "result": "ok"})))
k.subprocess.run = fr
check("probe: a good reply is ok", k.probe() == (True, ""))
check("probe uses Haiku, no tools, no saved session", "--model" in fr.cmd and fr.cmd[fr.cmd.index("--model") + 1] == k.MODEL and "--tools" in fr.cmd and "--no-session-persistence" in fr.cmd)
check("probe sends only the word ping", fr.kw.get("input") == "ping")
k.subprocess.run = with_run(cp(1, json.dumps({"is_error": True, "result": "Invalid API key. Please run /login"})))
ok, err = k.probe()
check("probe: an error reply is not ok and carries the message", not ok and "/login" in err)
k.subprocess.run = with_run(cp(0, json.dumps({"is_error": True, "result": "Not logged in"})))
check("probe: exit 0 with is_error is still a failure", k.probe()[0] is False)
k.subprocess.run = with_run(cp(1, "", "Error: something broke\n  badly"))
check("probe: stderr text is used and tidied to one line", k.probe() == (False, "Error: something broke badly"))
k.subprocess.run = with_run(cp(0, "garbage"))
check("probe: unreadable output is a failure", k.probe()[0] is False)
k.subprocess.run = with_run(exc=subprocess.TimeoutExpired("claude", 120))
check("probe: a timeout is reported", k.probe() == (False, "the check timed out"))
k.subprocess.run = with_run(exc=FileNotFoundError("no claude"))
check("probe: a missing Claude is reported", k.probe()[0] is False and "could not start" in k.probe()[1])
k.subprocess.run = real

bad = results.count(False)
print(f"\n{len(results) - bad}/{len(results)} checks passed")
sys.exit(1 if bad else 0)
