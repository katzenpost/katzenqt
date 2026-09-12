PYCRDT_URL := https://github.com/y-crdt/pycrdt.git
PYCRDT_REV := 7fc0f7330fd55a0e99b91683b2820ebcd02a50a2
THINCLIENT_VER := 0.0.24
PPRINTPP_VER := 0.4.0
PIP_VER := 24.2
MATURIN_VER := 1.8.2
RUSTIC_AUDIO_URL := https://github.com/katzenpost/Rustic_Audio_PyO3
RUSTIC_AUDIO_REV := 2158e2a5cc5e58f430b4c0bf2f5603d8909becc6

.PHONY: deb

deb:
	@$(MAKE) -C packaging/debian

PACKAGING_HELP += '' 'Debian packaging:' \
	'  make deb                   Build the packages in a container' \
	'  make -C packaging/debian   container, container-kpclientd,' \
	'                             container-pydeps, container-all, repro,' \
	'                             container-clean (DISTRO=, DISTROS=)'
