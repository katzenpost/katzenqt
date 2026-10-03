# SPDX-License-Identifier: AGPL-3.0-only

FROM ghcr.io/astral-sh/uv:python3.12-trixie AS uv

FROM ghcr.io/catthehacker/ubuntu:rust-24.04

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        libasound2-dev \
        libegl1 \
        libfontconfig1 \
        libgl1 \
        libpulse0 \
        libxcb-cursor0 \
        libxkbcommon0 \
        pkg-config \
        devscripts \
        equivs

ARG GO_VERSION=1.27.1
ARG GO_SHA256=63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445
ARG ACT_VERSION=0.2.89
ARG ACT_SHA256=0191d6f1f3b716b5c55820032605d05fc3c1cdbf581ebeff655019e5dd1524c0
ARG FORGEJO_RUNNER_VERSION=13.2.0
ARG FORGEJO_RUNNER_SHA256=fadaec897f5e6641c363f87ecaf75f866f99c81b7097ec6b433e4c48123fcad1
ARG WOODPECKER_CLI_VERSION=3.18.1
ARG WOODPECKER_CLI_SHA256=e7e78b0cc2f1172407d4e1439909bf8f55fb9b55f0fa79ae28338b195ecb56a5

COPY --from=uv /usr/local/bin/uv /usr/local/bin/uv
COPY --from=uv /usr/local/bin/uvx /usr/local/bin/uvx

RUN curl -fsSLo /tmp/go.tar.gz \
      "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
 && echo "${GO_SHA256}  /tmp/go.tar.gz" | sha256sum -c - \
 && tar -C /usr/local -xzf /tmp/go.tar.gz \
 && rm -f /tmp/go.tar.gz \
 && ln -s /usr/local/go/bin/go /usr/local/bin/go \
 && ln -s /usr/local/go/bin/gofmt /usr/local/bin/gofmt

WORKDIR /tmp/runners
RUN curl -fsSLO "https://github.com/nektos/act/releases/download/v${ACT_VERSION}/act_Linux_x86_64.tar.gz" \
 && echo "${ACT_SHA256}  act_Linux_x86_64.tar.gz" | sha256sum -c - \
 && tar -xzf act_Linux_x86_64.tar.gz act \
 && install -m 0755 act /usr/local/bin/act \
 && curl -fsSLO "https://code.forgejo.org/forgejo/runner/releases/download/v${FORGEJO_RUNNER_VERSION}/forgejo-runner-${FORGEJO_RUNNER_VERSION}-linux-amd64" \
 && echo "${FORGEJO_RUNNER_SHA256}  forgejo-runner-${FORGEJO_RUNNER_VERSION}-linux-amd64" | sha256sum -c - \
 && install -m 0755 "forgejo-runner-${FORGEJO_RUNNER_VERSION}-linux-amd64" /usr/local/bin/forgejo-runner \
 && curl -fsSLO "https://github.com/woodpecker-ci/woodpecker/releases/download/v${WOODPECKER_CLI_VERSION}/woodpecker-cli_linux_amd64.tar.gz" \
 && echo "${WOODPECKER_CLI_SHA256}  woodpecker-cli_linux_amd64.tar.gz" | sha256sum -c - \
 && tar -xzf woodpecker-cli_linux_amd64.tar.gz woodpecker-cli \
 && install -m 0755 woodpecker-cli /usr/local/bin/woodpecker-cli \
 && cd / && rm -rf /tmp/runners
WORKDIR /

RUN uv --version && act --version && forgejo-runner --version \
 && woodpecker-cli --version && go version && command -v dh \
 && dpkg-buildpackage --version | head -1
