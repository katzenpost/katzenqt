PYCRDT_URL := https://github.com/y-crdt/pycrdt.git
PYCRDT_REV := 7fc0f7330fd55a0e99b91683b2820ebcd02a50a2
THINCLIENT_VER := 0.0.24
PPRINTPP_VER := 0.4.0
PIP_VER := 24.2
MATURIN_VER := 1.8.2
RUSTIC_AUDIO_URL := https://github.com/katzenpost/Rustic_Audio_PyO3
RUSTIC_AUDIO_REV := 2158e2a5cc5e58f430b4c0bf2f5603d8909becc6

DEB_DISTROS ?= debian-13 debian-forky ubuntu-26.04

.PHONY: deb deb-ci deb-test deb-image-ref deb-image deb-image-push \
	deb-image-refs

deb:
	@$(MAKE) -C packaging/debian

deb-ci:
	@packaging/debian/ci.sh

deb-test:
	@packaging/debian/test.sh

deb-image-ref:
	@packaging/container/deb-image.sh --ref $(DISTRO)

deb-image-refs:
	@for d in $(DEB_DISTROS); do \
		key=$$(printf '%s' "$$d" | tr '.-' '__'); \
		printf 'ref_%s=%s\n' "$$key" \
			"$$(packaging/container/deb-image.sh --ref $$d)"; \
	done

deb-image:
	@packaging/container/deb-image.sh --ensure $(DISTRO)

deb-image-push:
	@packaging/container/deb-image.sh --push $(DISTRO)

PACKAGING_HELP += '' 'Debian packaging:' \
	'  make deb                   Build the packages in a container' \
	'  make deb-ci                Build, test, check reproducibility' \
	'  make deb-test              Test an installed package' \
	'  make deb-image             Build the CI image for DISTRO' \
	'  make deb-image-push        Build and push it for DISTRO' \
	'  make deb-image-ref         Print the image ref for DISTRO' \
	'  make deb-image-refs        Print every distro ref for CI' \
	'  make -C packaging/debian   container, container-kpclientd,' \
	'                             container-pydeps, container-all, repro,' \
	'                             container-clean (DISTRO=, DISTROS=)'
