"""Counters for the admin's stats panel.

COUNTERS ONLY: how many messages, how many characters, how many Claude tokens, how long answers took. Never the
text of a message, never what anyone said. Counts live in memory and are written to stats.json (private,
git-ignored, mode 600) every few minutes and on shutdown, so the SD card is not written to on every message.
"""
import contextvars, datetime, json, os, shutil, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
KEEP_DAYS = 35
SLOW_MS = 8000                     # an answer slower than this counts as "slow"
FLUSH_EVERY = float(os.environ.get("POLYGO_STATS_FLUSH_SECONDS", "300"))      # seconds between writes to disk
who = contextvars.ContextVar("who", default="")      # the device whose request is being served (set by the endpoints)

state = {"days": {}, "devices": {}}
dirty = False
started = time.time()


def path():
    return os.environ.get("POLYGO_STATS_FILE", os.path.join(HERE, "stats.json"))


def _key(d=None):
    return (d or datetime.date.today()).isoformat()


def _day():
    return state["days"].setdefault(_key(), {"messages": 0, "chars": 0, "calls": 0, "in": 0, "out": 0, "cw": 0, "cr": 0,
                                             "ms": 0, "slow": 0, "errors": 0})


def _dev(did):
    return state["devices"].setdefault(did, {"messages": 0, "chars": 0, "tokens": 0, "last": 0, "days": {}})


def _n(v):
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


# ---------- recording ----------
def message(did, chars):
    """One message was translated for this device (nothing about its content is kept except its length)."""
    global dirty
    d = _day()
    d["messages"] += 1
    d["chars"] += _n(chars)
    if did:
        v = _dev(did)
        v["messages"] += 1
        v["chars"] += _n(chars)
        v["last"] = time.time()
        v["days"].setdefault(_key(), [0, 0])[0] += 1
    dirty = True


def call(usage, ms):
    """One call to the engine finished. `usage` is the token count Claude reports for that turn."""
    global dirty
    u = usage if isinstance(usage, dict) else {}
    tin, tout = _n(u.get("input_tokens")), _n(u.get("output_tokens"))
    cw, cr = _n(u.get("cache_creation_input_tokens")), _n(u.get("cache_read_input_tokens"))
    d = _day()
    d["calls"] += 1
    d["in"] += tin
    d["out"] += tout
    d["cw"] += cw
    d["cr"] += cr
    d["ms"] += _n(ms)
    if ms > SLOW_MS:
        d["slow"] += 1
    did = who.get()
    if did:
        v = _dev(did)
        fresh = tin + tout + cw                    # what is not the cheap cached prompt
        v["tokens"] += fresh
        v["days"].setdefault(_key(), [0, 0])[1] += fresh
    dirty = True


def error():
    global dirty
    _day()["errors"] += 1
    dirty = True


# ---------- disk ----------
def load():
    global state, dirty
    try:
        with open(path(), encoding="utf-8") as f:
            data = json.load(f)
        state = {"days": data.get("days", {}), "devices": data.get("devices", {})}
    except (OSError, ValueError):
        state = {"days": {}, "devices": {}}
    dirty = False


def prune(keep_ids=None):
    cutoff = _key(datetime.date.today() - datetime.timedelta(days=KEEP_DAYS))
    state["days"] = {k: v for k, v in state["days"].items() if k >= cutoff}
    for did, v in list(state["devices"].items()):
        v["days"] = {k: x for k, x in v.get("days", {}).items() if k >= cutoff}
        if keep_ids is not None and did not in keep_ids and not v["days"]:
            del state["devices"][did]


def save(keep_ids=None):
    global dirty
    prune(keep_ids)
    p = path()
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p) or ".", prefix=".stats-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dirty = False


async def flush_forever(keep_ids):
    import asyncio
    while True:
        await asyncio.sleep(FLUSH_EVERY)
        if dirty:
            try:
                save(keep_ids())
            except OSError:
                pass


# ---------- reading ----------
def _pi():
    out = {}
    try:
        mem = {}
        for line in open("/proc/meminfo", encoding="utf-8"):
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0])
        out["mem_total_mb"], out["mem_available_mb"] = mem["MemTotal"] // 1024, mem["MemAvailable"] // 1024
    except Exception:
        pass
    try:
        out["cpu_temp_c"] = round(int(open("/sys/class/thermal/thermal_zone0/temp", encoding="utf-8").read()) / 1000, 1)
    except Exception:
        pass
    try:
        out["load1"] = round(os.getloadavg()[0], 2)
    except Exception:
        pass
    try:
        du = shutil.disk_usage(HERE)
        out["disk_free_gb"], out["disk_total_gb"] = round(du.free / 1e9, 1), round(du.total / 1e9, 1)
    except Exception:
        pass
    try:
        out["pi_uptime_s"] = int(float(open("/proc/uptime", encoding="utf-8").read().split()[0]))
    except Exception:
        pass
    out["service_uptime_s"] = int(time.time() - started)
    return out


def snapshot(device_list, engine, rooms):
    """Everything the panel shows. `device_list` is devices.listing()["devices"]."""
    today = datetime.date.today()
    keys = [_key(today - datetime.timedelta(days=i)) for i in range(29, -1, -1)]      # oldest first, 30 days
    zero = {"messages": 0, "chars": 0, "calls": 0, "in": 0, "out": 0, "cw": 0, "cr": 0, "ms": 0, "slow": 0, "errors": 0}
    per = [(k, state["days"].get(k, zero)) for k in keys]
    last7 = per[-7:]
    sum7 = lambda f: sum(v.get(f, 0) for _, v in last7)
    calls7 = sum7("calls")
    days = [{"date": k, "messages": v["messages"], "tokens": v["in"] + v["out"] + v["cw"]} for k, v in per[-14:]]
    seven = keys[-7:]
    rows, listed7 = [], 0
    for dv in device_list:
        v = state["devices"].get(dv["id"], {"messages": 0, "chars": 0, "tokens": 0, "last": 0, "days": {}})
        m7 = sum(v["days"].get(k, [0, 0])[0] for k in seven)
        t7 = sum(v["days"].get(k, [0, 0])[1] for k in seven)
        listed7 += m7
        rows.append({"id": dv["id"], "name": dv["name"], "lang": dv["lang"], "admin": dv["admin"],
                     "messages_7d": m7, "messages_total": v["messages"], "tokens_7d": t7, "tokens_total": v["tokens"],
                     "last_used": max(v.get("last", 0), dv.get("last_used", 0))})
    return {
        "totals": {"today": per[-1][1]["messages"], "week": sum7("messages"), "month": sum(v["messages"] for _, v in per),
                   "chars_week": sum7("chars"), "tokens_week": sum7("in") + sum7("out") + sum7("cw"), "cached_week": sum7("cr"),
                   "removed_week": max(0, sum7("messages") - listed7)},
        "days": days,
        "devices": rows,
        "engine": {**engine, "calls_week": calls7, "avg_ms": int(sum7("ms") / calls7) if calls7 else 0,
                   "slow_week": sum7("slow"), "errors_week": sum7("errors")},
        "rooms": rooms,
        "pi": _pi(),
    }
