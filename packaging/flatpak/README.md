# katzenqt Flatpak

This experimental developer package is maintained by the Katzenpost project.
Do not rely on it for security, anonymity, privacy, availability, delivery, or
data retention.

The sandbox has no network socket. katzenqt talks to the host `kpclientd`
daemon over its read-only Unix socket at `xdg-run/katzenpost`. The daemon runs
as a plain systemd user service on the host; the sandbox never starts it.

The manifest is x86-64-only until every compiled dependency is pinned for
aarch64. Bundled wheels keep their license files; review those licenses at
each tagged release.
