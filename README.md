# PolyGo

A small real-time family translator. Two people who share no language talk through one web page on their phones: the page listens, translates, shows the result and speaks it.

Built for Assamese ⇄ German (Hindi and English also work). Status: **working proof of concept**, used on a home network.

## How it works

```
phone browser ──► FastAPI (server.py) ──► one long-running Claude Code session (translator.py)
  mic: Web Speech API                        model: Sonnet, strict interpreter prompt (system_prompt.txt)
  voice: speechSynthesis  ◄── JSON {t, ok, from} ◄──┘
```

- **Speech in / out happens on the phone** (browser `SpeechRecognition` and `speechSynthesis`), so the server does no audio work and can be a small Raspberry Pi.
- **Language is auto-detected.** The page sends the two languages of the conversation; the model works out which one the text is in and translates into the other.
- **Assamese** can be written in Latin letters (the way the family chats, default) or Assamese script, switchable in the header. Chrome has no usable Assamese recogniser in the browser, so spoken Assamese is heard as Bengali (`bn-IN`, almost the same script) and the model reads it by sound.
- **Unsure? It says so.** The model returns `ok:false` plus an English rendering when it is not confident; the page shows a warning and does not auto-play it.
- One request runs at a time. The engine restarts after 40 turns or on a 45 s hang.

## Run it

Requires Python 3.11+ and the [Claude Code](https://claude.com/claude-code) CLI, logged in, on the machine that runs the server.

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
AT_MODEL=claude-sonnet-5-5 ./venv/bin/uvicorn server:app --host 0.0.0.0 --port 8765
```

Open `http://<host>:8765`. A systemd user unit is in `deploy/`.

## Known limits

- **The microphone needs HTTPS** (or `localhost`). On plain `http` only typing works; for testing on Chrome, allow the address in `chrome://flags/#unsafely-treat-insecure-origin-as-secure`. iPhone has no such flag, so it needs real HTTPS.
- **No login.** Anyone who can reach the port can use the translator, so keep it on a trusted network until an access token is added.
- Chrome's speech recogniser sends audio to Google. Speech output uses whatever voices the phone has installed; there is no Assamese voice on most phones, so Latin-letter Assamese is read by an Indian-English voice.
- Assamese translation quality has only been spot-checked, not measured. Treat the "unsure" flag as a hint, not a guarantee.
- The translation engine is the Claude Code CLI, not the API. `translator.py` is the only file that talks to it, so it can be swapped. Check Anthropic's current terms for how you are allowed to use it.
- `MemoryMax` in the unit file is not enforced on every Raspberry Pi setup (user cgroups may not track memory).
