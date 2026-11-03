"""Unit tests for the stats counters (no server needed):  python tests/stats_test.py"""
import datetime, json, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TMP = tempfile.mkdtemp()
os.environ["POLYGO_STATS_FILE"] = os.path.join(TMP, "stats.json")
import stats

results = []


def check(label, cond, extra=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + label + (("  -> " + str(extra)) if extra != "" else ""))


USAGE = {"input_tokens": 4, "output_tokens": 31, "cache_read_input_tokens": 5600, "cache_creation_input_tokens": 0}
DEVS = [{"id": "d1", "name": "Smita", "lang": "English", "admin": False, "last_used": 100.0},
        {"id": "d2", "name": "Mechthild", "lang": "German", "admin": False, "last_used": 200.0}]

stats.load()
check("starts empty", stats.state == {"days": {}, "devices": {}})
snap = stats.snapshot(DEVS, {"model": "m"}, {"open": 0, "people": 0})
check("an empty snapshot has 14 days, all zero, and both phones", len(snap["days"]) == 14 and all(d["messages"] == 0 for d in snap["days"]) and len(snap["devices"]) == 2)
check("the last day is today", snap["days"][-1]["date"] == datetime.date.today().isoformat())

stats.who.set("d1")
stats.message("d1", 12)
stats.call(USAGE, 1500)
stats.who.set("d2")
stats.message("d2", 30)
stats.call({"input_tokens": 5, "output_tokens": 100, "cache_read_input_tokens": 5600}, 9000)      # a slow one
stats.error()
stats.who.set("")
stats.call(None, 100)                                  # the warm-up call: no device, no usage
stats.call({"input_tokens": "x", "output_tokens": None}, 100)   # junk usage must not crash
snap = stats.snapshot(DEVS, {"model": "m"}, {"open": 1, "people": 2})
t, e = snap["totals"], snap["engine"]
check("messages counted today and in the week", t["today"] == 2 and t["week"] == 2 and t["month"] == 2, t)
check("characters counted, not text", t["chars_week"] == 42)
check("fresh tokens = input + output + cache writes (cached reads kept apart)", t["tokens_week"] == 4 + 31 + 5 + 100 and t["cached_week"] == 11200, t)
dv = {d["id"]: d for d in snap["devices"]}
check("tokens are attributed to the phone that asked", dv["d1"]["tokens_7d"] == 35 and dv["d2"]["tokens_7d"] == 105, (dv["d1"], dv["d2"]))
check("messages per phone", dv["d1"]["messages_7d"] == 1 and dv["d2"]["messages_total"] == 1)
check("engine: calls, slow, errors, average", e["calls_week"] == 4 and e["slow_week"] == 1 and e["errors_week"] == 1 and e["avg_ms"] == (1500 + 9000 + 100 + 100) // 4, e)
check("rooms are passed through", snap["rooms"] == {"open": 1, "people": 2})
snap2 = stats.snapshot(DEVS[:1], {}, {})
check("a removed phone's messages are counted apart", snap2["totals"]["removed_week"] == 1, snap2["totals"])

# disk
stats.state["days"]["2000-01-01"] = {"messages": 99}
stats.state["devices"]["gone"] = {"messages": 1, "chars": 1, "tokens": 1, "last": 1, "days": {"2000-01-01": [1, 1]}}
stats.save(["d1", "d2"])
raw = open(stats.path(), encoding="utf-8").read()
check("old days and unknown old phones are dropped when saving", "2000-01-01" not in raw and "gone" not in raw)
if os.name != "nt":
    check("the stats file is private (600)", oct(os.stat(stats.path()).st_mode & 0o777) == "0o600")
data = json.loads(raw)
allowed = {"messages", "chars", "calls", "in", "out", "cw", "cr", "ms", "slow", "errors"}
check("only counters are stored per day", all(set(v) <= allowed for v in data["days"].values()))
check("only counters are stored per phone", all(set(v) <= {"messages", "chars", "tokens", "last", "days"} for v in data["devices"].values()))
stats.state = {"days": {}, "devices": {}}
stats.load()
snap3 = stats.snapshot(DEVS, {}, {})
check("the numbers survive a restart", snap3["totals"]["week"] == 2 and snap3["devices"][0]["messages_total"] == 1)
check("a broken file starts clean instead of crashing", (open(stats.path(), "w").write("{nope"), stats.load(), stats.state)[2] == {"days": {}, "devices": {}})
check("the Pi readings never crash (missing files are just left out)", isinstance(stats._pi(), dict) and "service_uptime_s" in stats._pi())

bad = results.count(False)
print(f"\n{len(results) - bad}/{len(results)} checks passed")
sys.exit(1 if bad else 0)
