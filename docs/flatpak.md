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

## Starting the daemon

Outside the Flatpak the launcher can start the daemon itself. `python -m
katzenqt.launcher --install-service` writes the plain `Type=simple` unit from
package data, reloads the user manager, and enables and starts it; `make
kpclientd.service` builds and installs the binary and config and then calls the
launcher to do this. On a normal run, if no socket is reachable the launcher
installs and starts the service and waits for the socket. systemd owns
supervision through the unit's restart policy.

Inside the Flatpak the launcher never starts the daemon: a sandboxed GUI cannot
reach the host systemd user manager, so it only reaches an already running host
daemon over the socket.

## Releasing to Flathub

`make flatpak-release TAG=vX.Y.Z` validates that the tag is an annotated,
merged release whose version matches `pyproject.toml` and the metainfo, stages
the Flathub files from the tag, builds and lints them, and opens a pull request
against Flathub. It only reads the tag, so a dirty or unmatched worktree stops
the release early.
