.PHONY: flatpak-build flatpak-install flatpak-run flatpak-test \
	flatpak-system-deps

flatpak-build:
	@packaging/flatpak/build.sh

flatpak-install:
	@packaging/flatpak/install.sh

flatpak-run:
	@packaging/flatpak/run.sh

flatpak-test:
	@packaging/flatpak/test.sh

flatpak-system-deps:
	@packaging/flatpak/system-deps.sh

PACKAGING_HELP += '' 'Flatpak packaging:' \
	'  make flatpak-system-deps   Install the Flatpak build dependencies' \
	'  make flatpak-build         Build the Flatpak bundle' \
	'  make flatpak-install       Install the bundle for this user' \
	'  make flatpak-run           Run the installed Flatpak' \
	'  make flatpak-test          Run the Flatpak checks'
