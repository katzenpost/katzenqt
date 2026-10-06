# The Debian package

`katzenqt` builds as one binary package, architecture `all`, from
`debian/` in this repository. The source package is `katzenqt` and
`debian/control` declares that single binary package; there is no
separate daemon package here, and the client depends on `katzenpost`
for one.

## Building it

    make deb                        # DISTRO defaults to ubuntu-26.04
    make deb DISTRO=debian-13

`make deb` comes from `packaging/debian/targets.mk`, which the root
Makefile picks up through `-include packaging/*/targets.mk`, and it
delegates to `packaging/debian/Makefile`. That builds in a container,
never on the host: `packaging/container/build.sh` builds an image from
`packaging/container/Containerfile`, mounts the worktree read-only,
copies it inside, runs `packaging/debian/build.sh`, and copies the
result back to `dist/<distro>/`. It prints the sha256 of what it made.

Locally this works for the two distros that have a settings file in
`packaging/container/overrides/`: `debian-13.env` and
`ubuntu-26.04.env`. `build.sh` exits with `unknown distro` for anything
else, so `DISTRO=debian-forky` fails on a workstation even though CI
builds it. CI does not use those files at all (see below).

## What the build does

`debian/rules` is a four-line `dh` run with `--with python3
--buildsystem=pybuild`; `pyproject.toml` is flit, so
`pybuild-plugin-pyproject` is a build dependency. Two things are worth
knowing:

* `SOURCE_DATE_EPOCH` defaults to the timestamp of the top
  `debian/changelog` entry, so the same source produces the same bytes.
  `make -C packaging/debian repro` proves it by building twice in one
  container and comparing; `ci.sh` makes the same check with
  `packaging/debian/assert-reproducible.sh` over the built `.deb`s.
* `override_dh_auto_test` is empty. The test suite does not run at
  package build time; it runs in the pytest workflow instead.

## Dependencies

Everything in `Depends` comes from Debian or Ubuntu except three
things, and they are not all built in the same place.

`make -C packaging/debian container-pydeps` builds two of them as
`.deb`s of their own into `dist/<distro>/`: `python3-pycrdt` and
`python3-rustic-audio-tool`, each from a wheel built from a pinned git
revision. The third, `python3-katzenpost-thinclient`, is built by
`packaging/debian/ci.sh`, so a local `container-pydeps` does not
produce it. Every revision is pinned at the top of
`packaging/debian/targets.mk`, so a closure built today and one built
next month are the same closure.

`make -C packaging/debian container-all` builds that closure and then
the package itself, for every distro in `DISTROS`.

Two known gaps in `debian/control`, neither of them fixed by a build:

* the `Description` says the package depends on `python3-pycrdt` and
  `python3-katzenpost-thinclient`, but `Depends` does not list them.
  Install them from `dist/<distro>/` alongside the package.
* `PACKAGING_HELP` in `targets.mk` advertises a `container-kpclientd`
  target. `packaging/debian/Makefile` has no such target.

## The other targets

    make deb-ci          build, test, and check reproducibility
    make deb-test        test an already-installed package
    make deb-image       build the CI image for DISTRO
    make deb-image-ref   print the image reference for DISTRO
    make deb-image-refs  print every reference, for CI
    make -C packaging/debian container-clean

`make deb-ci` runs `packaging/debian/ci.sh`, which is the one that
expects to be inside a container: it installs build dependencies with
`mk-build-deps`, fetches pinned Go and Rust toolchains by sha256,
builds the katzenpost and thin-client revisions that the root Makefile
and `targets.mk` pin, installs the whole closure, runs
`packaging/debian/test.sh` against it, and finishes by asserting the
`.deb`s are reproducible.
