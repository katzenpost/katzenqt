# Development shell for NixOS (and Nix on any Linux).
#
#   nix-shell            # or: nix develop
#   uv sync
#   uv run katzenqt
#
# uv installs PySide6 from PyPI as manylinux wheels, which expect the
# Qt runtime libraries in FHS paths that NixOS doesn't have, so this
# shell puts them on LD_LIBRARY_PATH. It also points uv at nixpkgs'
# Python, because the Pythons uv downloads can't run on NixOS.
{ pkgs ? import <nixpkgs> { } }:

let
  python = pkgs.python312;

  # nixpkgs renamed the xorg.* libraries; take the new name where it
  # exists so older channels keep working.
  x11 = new: old: pkgs.${new} or pkgs.xorg.${old};

  qtRuntimeLibs = with pkgs; [
    stdenv.cc.cc.lib
    zlib
    zstd
    bzip2
    krb5
    libdrm
    glib
    dbus
    fontconfig
    freetype
    libGL
    libxkbcommon
    wayland
    (x11 "libx11" "libX11")
    (x11 "libxcb" "libxcb")
    (x11 "libxext" "libXext")
    (x11 "libxrandr" "libXrandr")
    (x11 "libxcb-util" "xcbutil")
    xcb-util-cursor
    (x11 "libxcb-wm" "xcbutilwm")
    (x11 "libxcb-image" "xcbutilimage")
    (x11 "libxcb-keysyms" "xcbutilkeysyms")
    (x11 "libxcb-render-util" "xcbutilrenderutil")
    libpulseaudio
    alsa-lib
  ];
in
pkgs.mkShell {
  packages = with pkgs; [
    python
    uv
    git
    gnumake
    go
    # rustic-audio-tool is built from source by uv sync.
    cargo
    rustc
    pkg-config
    alsa-lib
  ];

  env = {
    UV_PYTHON_PREFERENCE = "only-system";
    LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath qtRuntimeLibs;
  };
}
