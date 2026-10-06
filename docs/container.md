# Container builds

Nothing in the packaging path builds on the host. Every `.deb` is built
inside a container so the result does not depend on what happens to be
installed on a workstation, and so the same build runs in CI.

There are two separate mechanisms. They do the same thing and resolve
their base image differently, which is the first thing to know.

## The local mechanism

`make deb` and the targets under `make -C packaging/debian` read a
settings file per distro from `packaging/container/overrides/`:

    debian-13.env     BASE=docker.io/debian:13
    ubuntu-26.04.env  BASE=docker.io/ubuntu:26.04

Each file sets `BASE` and `BUILD_DEPS`. A file may also set `SKIP` with
a reason, which makes `build.sh` and `reproducible.sh` print it and
exit 0 instead of failing; `build-python-debs.sh` does not read it, and
neither override sets it today. A distro with no file is an error, so
only those two build locally.

Three images are involved:

    Containerfile              the package build
    Containerfile.python-debs  the not-in-Debian dependency closure
    Containerfile.ci           the image CI runs the build inside

`PODMAN` overrides the engine; the scripts default to `podman` and CI
passes `PODMAN=docker`.

## The CI mechanism

`.github/workflows/deb.yml` does not use the overrides. It builds for
three distros, `debian-13`, `debian-forky` and `ubuntu-26.04`, and
`packaging/container/deb-image.sh` derives the base image from the name
by turning the first dash into a colon: `debian-forky` becomes
`docker.io/debian:forky`. That is why CI can build a distro a
workstation cannot.

The image is content-addressed. `deb-image.sh` hashes `debian/control`
and `Containerfile.ci` together and uses the first twelve characters as
the tag, so the image is rebuilt only when the build dependencies or
the CI image definition change, and every later run pulls it:

    make deb-image-ref DISTRO=nonesuch-99    # print the reference
    make deb-image DISTRO=debian-13        # build it if it is missing
    make deb-image-push DISTRO=debian-13   # build and push it

`--ensure` skips the build when the manifest already exists, so a
repeat run costs one registry lookup.

The workflow then runs `make deb-ci` inside that image, once per
distro, and a final `deb-result` job turns the matrix into one verdict:
a skipped matrix is a pass, because the `changes` job skips the whole
thing when no packaging file was touched.

## Reproducibility

    make -C packaging/debian repro DISTRO=debian-13

`reproducible.sh` copies the worktree twice inside one container,
builds both, and compares. `SOURCE_DATE_EPOCH` comes from the
`debian/changelog` timestamp, so the two builds agree on every
timestamp they embed. The CI path makes the same check its last step,
through `packaging/debian/assert-reproducible.sh`.

## Cleaning up

    make -C packaging/debian container-clean

Build output lands in `dist/<distro>/` and is not cleaned by that
target; remove it by hand when a stale `.deb` would confuse you.
