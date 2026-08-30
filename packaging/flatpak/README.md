# katzenqt Flatpak

This experimental developer package is maintained by the Katzenpost project.
Do not rely on it for security, anonymity, privacy, availability, delivery, or
data retention.

The default sandbox denies direct network sockets. katzenqt requests activation
of the host user `kpclientd` and uses its read-only Unix socket. Wayland,
fallback X11, graphics, session D-Bus, and the daemon socket remain trusted
interfaces. A network-enabled launcher fallback is available only through an
explicit user override and removes the direct-network boundary.

Only annotated release tags whose version matches `pyproject.toml` and
AppStream metadata may be staged. The manifest is x86-64-only until every
compiled dependency is pinned for aarch64. Bundled wheels retain their license
metadata; their licenses must be reviewed as part of each tagged release.
