#!/bin/sh
set -eu
sudo apt install -y podman flatpak
flatpak remote-add --user --if-not-exists flathub \
	https://flathub.org/repo/flathub.flatpakrepo
