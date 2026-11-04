"""Keeps the Claude login alive.

The Claude CLI stays signed in by renewing its token each time it is used. If PolyGo sits unused for about a week the
renewal can lapse and the family would find translation dead. So: whenever a whole day (POLYGO_KEEPALIVE_HOURS) has
passed with no successful Claude call, one tiny Haiku call is made (about 550 tokens, a hundredth of a cent). A real
translation counts as use, so on a normal day this never fires. The last result is shown in the admin's Stats panel.

State is a few numbers in keepalive.json (private, git-ignored). It is saved at most about once an hour, and on shutdown,
so the SD card is not written to on every message. Never any message text.
"""
import asyncio, json, os, re, subprocess, tempfile, time

import translator

HERE = os.path.dirname(os.path.abspath(__file__))
HOURS = float(os.environ.get("POLYGO_KEEPALIVE_HOURS", "24"))                    # idle time that triggers a call
CHECK_EVERY = float(os.environ.get("POLYGO_KEEPALIVE_CHECK_SECONDS", "1800"))     # how often to look at the clock
FIRST_CHECK = float(os.environ.get("POLYGO_KEEPALIVE_FIRST_SECONDS", "60"))       # after start-up, so the network is up
MODEL = os.environ.get("POLYGO_KEEPALIVE_MODEL", "claude-haiku-5-5")
SAVE_EVERY = 3600
LOGIN_WORDS = re.compile(r"log ?in|authenticat|unauthori[sz]ed|\b401\b|expired|invalid.{0,20}(token|key)|credential|oauth", re.I)

state = {"last_ok": 0.0, "last_try": 0.0, "error": "", "login_problem": False}
_dirty, _saved = False, 0.0


def path():
    return os.environ.get("POLYGO_KEEPALIVE_FILE", os.path.join(HERE, "keepalive.json"))


def load():
    global _dirty
    try:
        with open(path(), encoding="utf-8") as f:
            d = json.load(f)
        state.update({"last_ok": float(d.get("last_ok", 0)), "last_try": float(d.get("last_try", 0)),
                      "error": str(d.get("error", ""))[:300], "login_problem": bool(d.get("login_problem", False))})
    except (OSError, ValueError, TypeError):
        pass
    _dirty = False


def save():
    global _dirty, _saved
    p = path()
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p) or ".", prefix=".keepalive-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _dirty, _saved = False, time.time()


def used_ok(now=None):
    """A real Claude call just worked, so the login is alive and renewed."""
    global _dirty
    state["last_ok"] = now or time.time()
    if state["error"] or state["login_problem"]:
        state["error"], state["login_problem"] = "", False
    _dirty = True


def due(now=None):
    return (now or time.time()) - state["last_ok"] >= HOURS * 3600


def probe():
    """One tiny Haiku call, outside the translator's own session. Returns (ok, error text)."""
    try:
        r = subprocess.run(
            [translator.CLAUDE, "-p", "--model", MODEL, "--tools", "", "--system-prompt", "Reply with the single word: ok",
             "--disable-slash-commands", "--strict-mcp-config", "--no-session-persistence", "--output-format", "json"],
            input="ping", capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, cwd=HERE)
    except subprocess.TimeoutExpired:
        return False, "the check timed out"
    except OSError as e:
        return False, "could not start Claude: " + str(e)[:150]
    out = (r.stdout or "").strip()
    try:
        d = json.loads(out)
    except ValueError:
        d = None
    if r.returncode == 0 and isinstance(d, dict) and not d.get("is_error"):
        return True, ""
    msg = (d.get("result") if isinstance(d, dict) and isinstance(d.get("result"), str) else "") or (r.stderr or "").strip() or out
    return False, " ".join(msg.split())[:300] or "Claude returned an error"


async def check_once(run=probe, now=None):
    """If a whole day passed with no successful call, make one. Returns "ok", "failed" or "skipped"."""
    global _dirty
    if not due(now):
        return "skipped"
    ok, err = await asyncio.to_thread(run)
    state["last_try"] = time.time()
    if ok:
        used_ok()
        res = "ok"
    else:
        state["error"], state["login_problem"] = err, bool(LOGIN_WORDS.search(err))
        res = "failed"
    _dirty = True
    save()
    return res


async def run_forever():
    await asyncio.sleep(FIRST_CHECK)
    while True:
        try:
            await check_once()
            if _dirty and time.time() - _saved >= SAVE_EVERY:
                save()
        except Exception:
            pass                        # a bad check must never take the server down; it is tried again next time
        await asyncio.sleep(CHECK_EVERY)


def snapshot():
    return {"last_ok": state["last_ok"], "last_try": state["last_try"], "error": state["error"],
            "login_problem": state["login_problem"], "idle_hours": HOURS}
