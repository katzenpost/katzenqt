# Packaging katzenqt

[Back to the README](../README.md)

katzenqt has Debian and Flatpak packaging. Neither is published yet; both are built
from this repository. The sources live in `debian/` and `packaging/`.

## Debian

```shell
make deb         # build the packages in a container
make deb-ci      # build, test, and check that the build is reproducible
make deb-test    # test an installed package
```

`make deb` builds the package in podman for `DISTRO` (default `ubuntu-26.04`) and writes
`dist/<distro>/katzenqt_*.deb`; CI builds for Debian 13, Debian forky and Ubuntu 26.04
(`DEB_DISTROS`). The `katzenqt` package
depends on the `katzenpost` package for `kpclientd`. Some of its Python dependencies are
not in Debian yet: `make -C packaging/debian container-pydeps` packages `pycrdt`, `hpqc`,
`highctidh` and `rustic-audio-tool`, and `make deb-ci` also packages the thin client,
each from the revision pinned in `packaging/debian/targets.mk`.

Related targets:

- `make check-version` checks that the three version statements agree, and
  `make check-version-test` tests that check.
- `make check-stale` reports pins that upstream has moved past.
- `make deb-image`, `make deb-image-push`, `make deb-image-ref` and `make deb-image-refs`
  manage the CI build images for `DISTRO`.
- `make -C packaging/debian` offers `container`, `container-kpclientd`,
  `container-pydeps`, `container-all`, `repro` and `container-clean`, with `DISTRO=` or
  `DISTROS=`.

## Flatpak

```shell
make flatpak-system-deps   # install the Flatpak build dependencies
make flatpak-build         # build the bundle
make flatpak-install       # install it for this user
make flatpak-run           # run the installed Flatpak
make flatpak-test          # run the Flatpak checks
```

The app id is `network.katzenpost.katzenqt`, on the KDE 6.9 runtime. The Flatpak does not
contain `kpclientd`: it reads the host daemon's socket at
`$XDG_RUNTIME_DIR/katzenpost/kpclientd.sock`, so the daemon must be running on the host,
as set up in [Building](building.md).
