PODMAN ?= podman
DISTRO ?= ubuntu-26.04
DISTROS ?= ubuntu-26.04 debian-13

.PHONY: container container-pydeps container-all \
	container-clean repro deb-image

container:
	@../container/build.sh $(DISTRO)

deb-image:
	@../container/deb-image.sh $(DISTRO)

container-pydeps:
	@../container/build-python-debs.sh $(DISTRO)

container-all:
	@for d in $(DISTROS); do \
		../container/build-python-debs.sh $$d; \
		../container/build.sh $$d; \
	done

repro:
	@PODMAN=$(PODMAN) ./reproducible.sh $(DISTRO)

container-clean:
	@PODMAN=$(PODMAN) ./container-clean.sh
