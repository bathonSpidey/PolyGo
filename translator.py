"""Translation engine: wraps Claude Code. Text in, dict out: {t, ok, from}."""
import json, os, re, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SYSTEM_PROMPT = open(os.path.join(HERE, "system_prompt.txt"), encoding="utf-8").read()
# Optional, private (git-ignored): the family's own words, names and example sentences.
_GLOSSARY = os.path.join(HERE, "glossary.txt")
if os.path.exists(_GLOSSARY):
    SYSTEM_PROMPT += ("\n\nFAMILY NOTES (written by the family, authoritative: follow them over your own guesses, "
                      "and copy their spellings of Assamese words):\n"
                      + open(_GLOSSARY, encoding="utf-8").read().strip() + "\n")
CLAUDE = os.path.expanduser("~/.npm-global/bin/claude")
FLAGS = ["--model", os.environ.get("AT_MODEL", "haiku"), "--tools", "", "--system-prompt", SYSTEM_PROMPT,
         "--disable-slash-commands", "--strict-mcp-config", "--no-session-persistence"]

def parse(raw):
    """Extract {"t","ok","from"}. The model sometimes corrects itself mid-reply, so the LAST valid
    JSON object wins; if there is none, treat the reply as not-ok."""
    for m in reversed(re.findall(r"\{[^{}]*\}", raw)):
        try:
            d = json.loads(m)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and "t" in d:
            return {"t": str(d.get("t", "")).strip(), "ok": bool(d.get("ok", False)), "from": d.get("from"),
                    "n": d.get("n") if isinstance(d.get("n"), str) else None}
    return {"t": raw.strip(), "ok": False, "from": None, "n": None}

class Session:
    """One long-running claude process; each translation is a new turn."""
    def __init__(self):
        self.p = subprocess.Popen(
            [CLAUDE, "-p", *FLAGS, "--input-format", "stream-json",
             "--output-format", "stream-json", "--verbose"],
            cwd=HERE, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def ask(self, content):
        msg = {"type": "user", "message": {"role": "user", "content": content}}
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        for line in self.p.stdout:
            ev = json.loads(line)
            if ev.get("type") == "result":
                return parse(ev.get("result") or "")
        raise RuntimeError("claude process ended")

    def translate(self, src, tgt, text):
        return self.ask(f"[{src} -> {tgt}] {text}")

    def translate_pair(self, a, b, text, assamese_script=False, tone="auto", latin_only=False, also_script=False):
        tags = ""
        if assamese_script:
            tags += "[assamese-output=script] "
        elif also_script:                       # Latin text for the screen + script for an Assamese/Bengali voice
            tags += "[also-script] "
        if tone in ("formal", "informal"):      # "auto" = no tag: mirror the speaker
            tags += f"[tone={tone}] "
        if latin_only:                          # retry after a stray non-Latin character
            tags += "[latin-only] "
        return self.ask(f"[{a} <-> {b}] {tags}{text}")

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)

    def kill(self):
        try:
            self.p.kill()
            self.p.wait(timeout=5)
        except Exception:
            pass
