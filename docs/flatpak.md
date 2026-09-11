# katzenqt Flatpak

The Flatpak packaging lives in `packaging/flatpak/`. It builds katzenqt against
the KDE 6.9 runtime with no network in the sandbox; the app reaches the host
`kpclientd` over its Unix socket.

The build runs inside a podman container that carries the whole flatpak
toolchain (flatpak-builder, ostree, the KDE runtime), so the host only needs
podman for the build and flatpak to install and run the result. The container
image and the `podman run` invocation live in `packaging/flatpak/container/`.

Run these from the repo root:

- `make flatpak-system-deps`  Install podman and flatpak and add the Flathub
  user remote. This is the only target that changes your system, so run it by
  hand once. The KDE runtime is pulled from Flathub the first time you install.
- `make flatpak-build`        Build the Flatpak. It checks that podman is
  present and points you at `flatpak-system-deps` otherwise, then builds and
  runs every check inside the container: it archives the committed `HEAD` (not
  the worktree, so commit first), lints the manifest, checks the build is
  reproducible, and validates the desktop and metainfo files.
- `make flatpak-install`      Install the built Flatpak for your user.
- `make flatpak-run`          Run the installed Flatpak.
- `make flatpak-test`         Test the installed Flatpak against the Katzenpost
  docker testnet. It first checks the sandbox (imports, no network, read-only
  socket) and then runs the integration suite through the sandboxed Python.

Python dependencies are vendored as pinned, hash-checked sources
(`python3-deps.json`, `pyside6-sources.json`) because the build has no network.
