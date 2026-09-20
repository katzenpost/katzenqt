This is a minimal containerized development environment to allow a developer to
work on katzenqt on a remote server and access it via a webserver.

Requirements:
* `apt install make podman podman-compose` (tested on a Debian trixie host; should work elsewhere with recent podman releases)

Running:
1. `cd webtop`
2. `make start`
3. If running remotely, forward port 3000 to your local system, eg `ssh -L 3000:127.0.0.1:3000 my_remote_host`
4. Connect to http://127.0.0.1:3000 locally to access webtop desktop
5. Open a terminal inside the webtop desktop and run
   `cd /config/katzenqt/webtop && make launch-3` to start three katzenqt
   clients (KQT_STATE a/b/c). Each dials kpclientd via TCP on
   `host_localhost:64331` (see `thinclient-webtop.toml` in this directory),
   which is the docker mixnet's kpclientd published on the host loopback.
   Client logs land at `/config/katzenqt/{a,b,c}.log`. The container's
   venv lives at `/config/.venv-katzenqt` (via `UV_PROJECT_ENVIRONMENT`),
   independent of the repo's host-side `.venv`.

There is obviously room for improvement in step 5; I am not opening a PR to
merge this until I work on this more but I am committing this now since I'm
currently using it.

