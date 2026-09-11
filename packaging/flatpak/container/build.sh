#!/bin/sh
set -eu

here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
root=$(CDPATH= cd -- "$here/../../.." && pwd)
cd "$root"

PODMAN="${PODMAN:-podman}"
image=katzenqt-flatpak

"$PODMAN" build -t "$image" \
	-f packaging/flatpak/container/Containerfile packaging/flatpak/container

# --privileged and --device /dev/fuse are required, not convenience: inside the
# container flatpak-builder builds each module under bubblewrap, which needs
# user namespaces that an unprivileged nested container does not have. Running
# flatpak-builder in a privileged container is the upstream way:
#   - the official flatpak-github-actions Action runs it in a container
#     declared `options: --privileged`
#     (https://github.com/flatpak/flatpak-github-actions)
#   - the Flathub build/CI docs use the same setup (https://docs.flathub.org/)
# The only alternative is building on the host, which this deliberately avoids.
# The repo is bind-mounted at /src; the build writes .flatpak-repo (and the
# other .flatpak-* dirs, all git-ignored) back into it.
"$PODMAN" run --rm --privileged --device /dev/fuse \
	-v "$root:/src:z" -w /src \
	-e KQT_FLATPAK_INNER=1 \
	"$image" packaging/flatpak/build.sh
