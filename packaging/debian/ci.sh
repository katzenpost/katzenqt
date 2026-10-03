#!/bin/sh
set -eu

debs=${DEBS_DIR:-/tmp/debs}
GO_VERSION=${GO_VERSION:-1.27.1}
go_sum=63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445
GO_SHA256=${GO_SHA256:-$go_sum}
RUST_VERSION=${RUST_VERSION:-1.99.0}
rust_sum=de0581ca9d732295a6474cfbd02461db27d69acd5050a8206523a8d6fa1599db
RUST_SHA256=${RUST_SHA256:-$rust_sum}
KATZENPOST_URL=${KATZENPOST_URL:-$(sed -n \
    's/^KATZENPOST_URL := //p' Makefile)}
KATZENPOST_REV=${KATZENPOST_REV:-$(sed -n \
    's/^KATZENPOST_REV := //p' Makefile)}
mkdir -p "$debs"

apt update
apt install -y --no-install-recommends devscripts equivs
mk-build-deps -ir -t "apt-get -y --no-install-recommends" debian/control

apt install -y --no-install-recommends \
    build-essential cargo cmake curl git libasound2-dev pkg-config \
    python3-dev python3-pip python3-venv rustc systemd unzip

if ! go version 2>/dev/null | grep -q "go$GO_VERSION"; then
    arch=$(dpkg --print-architecture)
    curl -fsSLo /tmp/go.tar.gz \
        "https://go.dev/dl/go$GO_VERSION.linux-$arch.tar.gz"
    echo "$GO_SHA256  /tmp/go.tar.gz" | sha256sum -c -
    rm -rf /usr/local/go
    tar -C /usr/local -xzf /tmp/go.tar.gz
fi
PATH=/usr/local/go/bin:$PATH
export PATH

if ! cargo --version 2>/dev/null \
     | awk '{split($2,v,"."); exit !(v[1]>1 || v[2]>=85)}'; then
    arch=$(dpkg --print-architecture)
    case $arch in amd64) rust_arch=x86_64-unknown-linux-gnu ;;
        arm64) rust_arch=aarch64-unknown-linux-gnu ;;
        *) printf '%s\n' "no pinned rust for $arch" >&2; exit 1 ;;
    esac
    rust_base=https://static.rust-lang.org/dist
    curl -fsSLo /tmp/rust.tar.gz \
        "$rust_base/rust-$RUST_VERSION-$rust_arch.tar.gz"
    echo "$RUST_SHA256  /tmp/rust.tar.gz" | sha256sum -c -
    tar -C /tmp -xzf /tmp/rust.tar.gz
    /tmp/rust-$RUST_VERSION-$rust_arch/install.sh --prefix=/usr/local \
        --components=rustc,cargo,rust-std-$rust_arch >/dev/null
fi

packaging/container/pydeps-build.sh "$debs"
packaging/container/dep-deb.sh "$debs" "$KATZENPOST_URL" "$KATZENPOST_REV"
packaging/debian/build.sh
cp dist/katzenqt_*.deb "$debs"/

apt update
apt install -y \
    "$debs"/python3-pprintpp_*.deb \
    "$debs"/python3-pycrdt_*.deb \
    "$debs"/python3-rustic-audio-tool_*.deb \
    "$debs"/python3-katzenpost-thinclient_*.deb \
    "$debs"/katzenpost_*.deb \
    "$debs"/katzenqt_0.0.1_all.deb

packaging/debian/test.sh
packaging/debian/assert-reproducible.sh "$debs"
