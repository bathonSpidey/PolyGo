"""Translation engine: wraps Claude Code. Text in, dict out: {t, ok, from, n}."""
import json, os, re, shutil, subprocess

import corrections

HERE = os.path.dirname(os.path.abspath(__file__))
# Where the Claude CLI is: an explicit setting, else whatever is on the PATH, else the usual npm location.
CLAUDE = os.environ.get("POLYGO_CLAUDE") or shutil.which("claude") or os.path.expanduser("~/.npm-global/bin/claude")
MODEL = os.environ.get("POLYGO_MODEL", "claude-sonnet-5-5")
# How hard the model "thinks" before answering: low is much faster, high is the CLI's default. Unset or
# unknown = leave the CLI alone (a typo must never stop the translator from starting).
EFFORTS = ("low", "medium", "high", "xhigh", "max")
EFFORT = os.environ.get("POLYGO_EFFORT", "").strip().lower()


def build_system_prompt():
    """Read fresh every time a session starts, so a saved correction applies from the next session on."""
    prompt = open(os.path.join(HERE, "system_prompt.txt"), encoding="utf-8").read()
    glossary = os.path.join(HERE, "glossary.txt")        # optional, private (git-ignored)
    if os.path.exists(glossary):
        prompt += ("\n\nFAMILY NOTES (written by the family, authoritative: follow them over your own guesses, "
                   "and copy their spellings of Assamese words):\n"
                   + open(glossary, encoding="utf-8").read().strip() + "\n")
    return prompt + corrections.render()


def _flags():
    # No tools, no MCP servers, no slash commands: the session can only produce text.
    flags = ["--model", MODEL, "--tools", "", "--system-prompt", build_system_prompt(),
             "--disable-slash-commands", "--strict-mcp-config", "--no-session-persistence"]
    if EFFORT in EFFORTS:
        flags += ["--effort", EFFORT]
    return flags


def parse(raw):
    """Extract {"t","ok","from","n"}. The model sometimes corrects itself mid-reply, so the LAST valid
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


def _json_objects(raw):
    """Every JSON object in the text, including nested ones, in order."""
    dec, i, out = json.JSONDecoder(), 0, []
    while True:
        i = raw.find("{", i)
        if i < 0:
            return out
        try:
            obj, end = dec.raw_decode(raw, i)
            out.append(obj)
            i = end
        except json.JSONDecodeError:
            i += 1


def parse_multi(raw):
    """For the '=>' form: {"tr": {language: text, ...}, "ok", "from", "n"}. The last valid object wins."""
    for d in reversed(_json_objects(raw)):
        if isinstance(d, dict) and isinstance(d.get("tr"), dict):
            tr = {str(k): v.strip() for k, v in d["tr"].items() if isinstance(v, str) and v.strip()}
            return {"tr": tr, "ok": bool(d.get("ok", False)), "from": d.get("from"),
                    "n": d.get("n") if isinstance(d.get("n"), str) else None}
    return {"tr": {}, "ok": False, "from": None, "n": None}


class Session:
    """One long-running claude process; each translation is a new turn."""
    last_usage = {}

    def __init__(self):
        self.p = subprocess.Popen(
            [CLAUDE, "-p", *_flags(), "--input-format", "stream-json",
             "--output-format", "stream-json", "--verbose"],
            cwd=HERE, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1)   # utf-8 always: Assamese, Hindi, German

    def ask(self, content, multi=False):
        msg = {"type": "user", "message": {"role": "user", "content": content}}
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        for line in self.p.stdout:
            ev = json.loads(line)
            if ev.get("type") == "result":
                self.last_usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}    # token counts for the stats panel
                raw = ev.get("result") or ""
                return parse_multi(raw) if multi else parse(raw)
        raise RuntimeError("claude process ended")

    @staticmethod
    def _tags(tone, latin_only, also_script):
        tags = ""
        if also_script:
            tags += "[also-script] "
        if tone in ("formal", "informal"):
            tags += f"[tone={tone}] "
        if latin_only:
            tags += "[latin-only] "
        return tags

    def translate_to(self, src, tgt, text, tone="auto", also_script=False, latin_only=False):
        """One known direction, e.g. for a room: the speaker's language is known, no guessing."""
        tags = self._tags(tone, latin_only, also_script and tgt == "Assamese")
        return self.ask(f"[{src} -> {tgt}] {tags}{text}")

    def translate_multi(self, src, targets, text, tone="auto", latin_only=False):
        """One known source language, several target languages in a single call."""
        tags = self._tags(tone, latin_only, "Assamese" in targets)
        return self.ask(f"[{src} => {', '.join(targets)}] {tags}{text}", multi=True)

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
