"""POLYGO_EFFORT only adds a flag when it is a known level:  python tests/effort_test.py"""
import importlib, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
results = []


def check(label, cond, extra=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + label + (("  -> " + str(extra)) if extra != "" else ""))


def flags_with(value):
    if value is None:
        os.environ.pop("POLYGO_EFFORT", None)
    else:
        os.environ["POLYGO_EFFORT"] = value
    import translator
    importlib.reload(translator)
    return translator._flags()


f = flags_with(None)
check("unset: no --effort flag", "--effort" not in f)
f = flags_with("low")
check("low: --effort low", f[f.index("--effort") + 1] == "low" if "--effort" in f else False)
f = flags_with(" Medium ")
check("spaces and capitals are forgiven", "--effort" in f and f[f.index("--effort") + 1] == "medium")
f = flags_with("fast")
check("unknown level is ignored, not passed on", "--effort" not in f)
f = flags_with("")
check("empty: no --effort flag", "--effort" not in f)
check("other flags stay", "--model" in f and "--no-session-persistence" in f)
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
