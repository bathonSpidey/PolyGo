#!/bin/bash
# Gives the Pi's Wi-Fi setup hotspot (comitup) a name and a password.
#
#   sudo bash ~/polygo/deploy/set-hotspot.sh            # name "PolyGo-Setup"
#   sudo bash ~/polygo/deploy/set-hotspot.sh MyName     # another name
#
# The password is read from ~/.hotspot-pass (never from the command line, so it does not end up in your shell
# history) and written into /etc/comitup.conf, which is made readable by root only. The password file is
# deleted afterwards. A reboot is needed for the new password to take effect.
set -euo pipefail

CONF="${COMITUP_CONF:-/etc/comitup.conf}"
NAME="${1:-PolyGo-Setup}"
if [ -z "${HOTSPOT_PWFILE:-}" ]; then
  [ "$(id -u)" = 0 ] || { echo "Run this with sudo."; exit 1; }
  USER_NAME="${SUDO_USER:-spidey}"
  PWFILE="$(getent passwd "$USER_NAME" | cut -d: -f6)/.hotspot-pass"
else
  PWFILE="$HOTSPOT_PWFILE"                      # test mode: a dummy file and a copy of the config
fi

[ -f "$PWFILE" ] || { echo "Missing $PWFILE"; exit 1; }
[ -f "$CONF" ] || { echo "comitup is not installed (no $CONF). Install it first:  sudo apt install comitup"; exit 1; }
cp -n "$CONF" "$CONF.orig-polygo" 2>/dev/null || true     # keep the original once

python3 - "$PWFILE" "$CONF" "$NAME" <<'PY'
import os, re, sys

pwfile, conf, name = sys.argv[1:4]
pw = open(pwfile, encoding="utf-8").read().rstrip("\r\n")
if not 8 <= len(pw) <= 63:
    sys.exit(f"The hotspot password must be 8 to 63 characters (this one has {len(pw)}).")
if pw != pw.strip() or any(ord(c) < 32 or ord(c) > 126 for c in pw):
    sys.exit("The password must be plain printable characters, with no space at either end.")
if "#" in pw:
    sys.exit("The password contains '#', which the config file would read as a comment. Choose another.")
if not re.fullmatch(r"[A-Za-z0-9._-]{1,28}", name):
    sys.exit("The hotspot name may only use letters, digits and . _ -")

lines = open(conf, encoding="utf-8").read().splitlines()


def set_key(key, value):
    for i, line in enumerate(lines):
        if re.match(rf"\s*{key}\s*:", line):          # a real (uncommented) setting: replace it
            lines[i] = f"{key}: {value}"
            return
    lines.append(f"{key}: {value}")                   # otherwise add it at the end


set_key("ap_name", name)
set_key("ap_password", pw)
os.chmod(conf, 0o600)                                 # root-only BEFORE the password goes in
with open(conf, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
os.chmod(conf, 0o600)
print(f"Hotspot '{name}' configured. {conf} can now be read by root only.")
PY

shred -u "$PWFILE" 2>/dev/null || rm -f "$PWFILE"
echo "Password file deleted."
echo "Now reboot so the new password takes effect:   sudo reboot"
