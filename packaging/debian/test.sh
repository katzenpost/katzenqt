#!/bin/sh
set -eu

katzenqt-headless --help >/dev/null
python3 -c "import pycrdt, katzenpost_thinclient"
test -x /usr/bin/kpclientd
test -f /etc/katzenpost/kpclientd.toml
test -f /usr/lib/systemd/user/kpclientd.service

units="/etc/systemd/user /usr/lib/systemd/user /root/.config/systemd/user"
if find $units -path '*.wants/kpclientd.service' 2>/dev/null | grep -q .; then
    printf '%s\n' "error: kpclientd.service is enabled" \
        "it must be opt-in per user" >&2
    exit 1
fi
