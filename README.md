# PolyGo

A small real-time family translator. Two people who share no language talk through one web page on their phones: it listens, translates, shows the result and speaks it. Built for **Assamese ⇄ German** (Hindi and English also work).

**Status:** working. Runs on a dedicated Raspberry Pi 4, reachable from anywhere over a public HTTPS link behind a family passcode. Next: a Wi-Fi setup hotspot so the Pi can travel, and the iPhone test.

## How a translation works

```mermaid
sequenceDiagram
    participant P as Phone browser
    participant S as PolyGo server (FastAPI)
    participant C as Claude Code session
    P->>P: tap mic, words appear live (Web Speech API)
    P->>S: text, the two languages, tone (needs the passcode)
    S->>C: translate between the pair
    C-->>S: translation, ok flag, detected language
    S-->>P: translation + which side spoke
    P->>P: show it, speak it (phone voices)
    P->>S: "Wrong?" with the right wording (optional)
    S->>S: save the correction, start a fresh session that knows it
```

```mermaid
flowchart LR
    P["Phones: mic and speaker"] -->|"HTTPS (Tailscale Funnel)"| W
    subgraph PI["Raspberry Pi 4"]
      direction TB
      W["PolyGo server, localhost only"] --> C["Claude Code session: no tools, no MCP"]
    end
    C -->|internet| A[Anthropic]
    P -.->|"speech recognition (Chrome)"| G[Google]
```

## What it does

- **Auto-detects** which of the two languages was said and translates into the other.
- **Tone** dropdown: formal (Assamese *apuni*, German *Sie*), informal (*tumi*, *du*) or match the speaker. The intimate forms *tur/tar/tahator* are never used.
- **Assamese** is shown in Latin letters (how the family chats), or in script via a header switch. If the phone has an Assamese or Bengali voice, it speaks the script version; otherwise an Indian-English voice reads the Latin text. Spoken Assamese is recognised as Bengali (`bn-IN`, almost the same script) because Chrome's `as-IN` returned English-looking text; the model reads it by sound.
- **Unsure?** The model says so (`ok:false`) and answers in English; the page warns and does not auto-play it.
- **Family glossary** (`glossary.txt`, private): names, relatives, time words, grammar rules (*tumi … asa*, *apuni … ase*) and example sentences the model must follow.
- **Learns as we go:** a **Wrong?** button on every answer lets anyone type the right wording. It is saved (`corrections.jsonl`, private, newest 200 kept), a fresh session picks it up, and **Settings → Saved corrections** lists and deletes them.
- **Simple view** for parents: two huge mic buttons named in each language's own script (অসমীয়া, Deutsch), a typing box with a send arrow, big text, a banner that says Listening / Translating / Speaking, and a **⋯** menu for text size. Answers are always spoken. Switch it on from the header, or by opening the page with `#simple` (`#full` goes back).
- **Invite link and QR code:** in the full view, **Settings → Invite someone** makes a link and a QR code that set up another phone: languages, tone, Latin or script, simple or full view and the passcode. The setup travels after the `#` (never sent to a server) and the page erases it from the address bar; the QR is drawn by the server itself (`/api/qr`, `segno`), so the passcode never goes to an outside QR service.
- **Shared conversations (rooms)** *(server side done, screens next)*: one person starts a room and shares its link; each phone joins in its own language and every message arrives translated into each person's language, so different languages can mix in one conversation. A message goes to the translator once, into all needed languages in a single call. Rules: a link lasts 24 h and still needs the family passcode; 2 to 10 phones (default 4, changeable by the creator, ceiling `POLYGO_ROOM_MAX`); latecomers see nothing from before they joined; everything stays in memory only (never on disk, a restart ends all rooms); at most 8 messages per person per 30 s. Delivery is long-polling (`/api/rooms/{id}/events`), so a phone that drops out simply asks again from where it was.
- Mic, auto-play, play button, speaking speed, one-request-at-a-time queue, automatic restart after 40 turns or a hang.

## Setup

Needs Python 3.11+ and the [Claude Code](https://claude.com/claude-code) CLI, logged in, on the machine that runs the server.

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
echo "pick-a-passcode" > passcode.txt && chmod 600 passcode.txt     # the server refuses to work without it
POLYGO_MODEL=claude-sonnet-5-5 ./venv/bin/uvicorn server:app --host 127.0.0.1 --port 8765
```

A systemd user unit is in `deploy/polygo-api.service`. Private, git-ignored files: `passcode.txt`, `glossary.txt` (start from `glossary.example.txt`), `corrections.jsonl`.

### Public HTTPS link (Tailscale Funnel)

Browsers only allow the microphone on HTTPS. [Tailscale Funnel](https://tailscale.com/kb/1223/funnel) gives a free `*.ts.net` address with a real certificate, no domain and no open router port.

```bash
# on the Pi, once (needs sudo)
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                      # prints a login link
sudo tailscale set --operator=$USER
# then, without sudo
tailscale funnel --bg 8765             # first run prints a link to enable Funnel/HTTPS in the admin page
```

A brand-new name can take a while to show up in public DNS. Before travelling, disable key expiry for the device in the Tailscale admin page.

## Security

| Threat | Protection |
|---|---|
| Strangers use the translator | Every translate, reset and correction call needs the family passcode (constant-time check; wrong guesses are serialised to about one per second overall, a correct one is never slowed). No passcode file means the API refuses everything. |
| Someone on the same Wi-Fi reaches the app | It listens on `127.0.0.1` only; only the Funnel link reaches it. |
| The model is tricked by the text it translates | The Claude session starts with no tools, no MCP servers and no slash commands, so it can only produce text. Injection attempts are translated, not obeyed. |
| Hostile text in a correction | Corrections are flattened to one line, length-capped, quoted and labelled as data before they reach the model; only passcode holders can save them. |
| API docs reveal the API | `/docs`, `/redoc`, `/openapi.json` are disabled. |
| A room link leaks | The link alone does not get anyone in: every room call needs the family passcode too. A room id is 128 random bits, a room dies after 24 h or when its creator ends it, size and message rate are capped, and nothing is stored on disk. |
| An invite link leaks | It contains the passcode, so anyone who gets the link or QR can use PolyGo: share it with family only. It is never sent to a server and is wiped from the address bar, but it can sit in a chat history. Changing the passcode (edit `passcode.txt`, restart) invalidates every old link. A planned upgrade is one-time invites that give each phone its own revocable key. |

Honest limits: the passcode is one shared secret, so treat it like a Wi-Fi password. The Claude login on the Pi cannot be restricted to "translator only"; if the Pi is lost, sign out all Claude sessions and remove the device in Tailscale. Chrome's speech recogniser sends audio to Google. The engine is the Claude Code CLI, not the API; `translator.py` is the only file that touches it, so it can be swapped (check Anthropic's current terms for how you may use it).

## Roadmap

- [x] Translation, tone, glossary, mic, voices, passcode, public HTTPS link, "Wrong?" corrections
- [x] Parent-friendly, step 1 and 2: simple view with typing box, invite link and QR
- [x] Shared conversations, step A and B: rooms on the server with joining, capacity, expiry, per-person languages and live delivery (40 automated checks)
- [ ] Shared conversations, next: the screens (start, share link/QR, join with the device language, one mic per phone, speak other people's messages), then limits tuning
- [ ] Parent-friendly, later: clearer errors and keep-awake, **quick phrases** (verified, instant, optionally in a family voice), app labels in Assamese and German, home-screen icon
- [ ] iPhone test over the public link (mic, speech, auto-play)
- [ ] Android: check which voices the phone offers (the page shows the voice used)
- [ ] **Wi-Fi setup hotspot** so the Pi can join new networks (flow below), protected by its own password
- [ ] Optional: tailnet-only access instead of the public link; firewall
- [ ] Parked: a Pi Zero 2W belt box (too little RAM for the Claude CLI), enclosure

### Planned: Wi-Fi setup hotspot (comitup)

The Pi only needs internet, from anywhere; phones reach it through the public link with their own data. When it finds no Wi-Fi it knows, it opens a password-protected hotspot, like an IoT device.

```mermaid
flowchart TD
    A[Power on] --> B{Known Wi-Fi in range?}
    B -- yes --> C[Join it]
    B -- no --> D["Start the setup hotspot, protected by its own password"]
    D --> E["Phone joins the hotspot, setup page opens"]
    E --> F["Pick a Wi-Fi or phone hotspot, enter its password"]
    F --> G["Pi saves it, stops the hotspot, joins"]
    G --> C
    C --> H{Internet reachable?}
    H -- yes --> I[PolyGo is reachable on the public link]
    H -- no --> D
```
