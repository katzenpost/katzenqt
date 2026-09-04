This is a minimal containerized development environment to allow a developer to
work on katzenqt on a remote server and access it via a webserver.

Requirements:
* `apt install make podman podman-compose` (tested on a Debian trixie host; should work elsewhere with recent podman releases)

Running:
1. `cd webtop`
2. `make start`
3. If running remotely, forward port 3000 to your local system, eg `ssh -L 3000:127.0.0.1:3000 my_remote_host`
4. Connect to http://127.0.0.1:3000 locally to access webtop desktop
5. edit thinclient.toml to point to a tcp listener on `host_localhost` on the port your docker test network has a thinclient listening on (consult `./docker/mixnet-alpine/client/thinclient.toml` in your katzenpost docker testnet running on the same machine as the webtop).
6. In the webtop environment, `cd katzenqt; KQT_STATE=alice uv run katzenqt`

There is obviously room for improvement in step 5; I am not opening a PR to
merge this until I work on this more but I am committing this now since I'm
currently using it.

