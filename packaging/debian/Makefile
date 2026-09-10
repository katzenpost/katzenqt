PODMAN ?= podman
DISTRO ?= ubuntu-26.04
DISTROS ?= ubuntu-26.04 debian-13

.PHONY: container container-kpclientd container-all container-clean

container:
	@../container/build.sh $(DISTRO)

container-kpclientd:
	@../container/build-kpclientd.sh $(DISTRO)

container-all:
	@for d in $(DISTROS); do \
		../container/build-kpclientd.sh $$d; \
		../container/build.sh $$d; \
	done

container-clean:
	@PODMAN=$(PODMAN) ./container-clean.sh
