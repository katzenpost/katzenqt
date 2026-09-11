#!/bin/sh
set -eu
# This is the only step that changes your system. Run it by hand once.
# The build runs inside podman; flatpak stays on the host only to install and
# run the built app. The KDE runtime is pulled from Flathub on first install.
sudo apt install -y podman flatpak
flatpak remote-add --user --if-not-exists flathub \
	https://flathub.org/repo/flathub.flatpakrepo
