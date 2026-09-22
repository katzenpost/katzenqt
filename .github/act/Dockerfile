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
        pkg-config
