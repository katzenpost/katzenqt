#!/bin/sh
set -eu
exec flatpak run network.katzenpost.katzenqt "$@"
