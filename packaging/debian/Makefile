PODMAN ?= podman
DISTRO ?= ubuntu-26.04
DISTROS ?= ubuntu-26.04 debian-13

.PHONY: container container-kpclientd container-pydeps container-all container-clean repro

container:
	@../container/build.sh $(DISTRO)

container-kpclientd:
	@../container/build-kpclientd.sh $(DISTRO)

container-pydeps:
	@../container/build-python-debs.sh $(DISTRO)

container-all:
	@for d in $(DISTROS); do \
		../container/build-python-debs.sh $$d; \
		../container/build-kpclientd.sh $$d; \
		../container/build.sh $$d; \
	done

repro:
	@PODMAN=$(PODMAN) ./reproducible.sh $(DISTRO)

container-clean:
	@PODMAN=$(PODMAN) ./container-clean.sh
