.PHONY: start stop clean

start:
	HOST_GID=$(shell id -g) HOST_UID=$(shell id -u) podman-compose up -d --build
stop:
	podman-compose down
clean:
	rm -rf ./uv_cache
