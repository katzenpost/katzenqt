# Testing katzenqt

[Back to the README](../README.md)

## Unit tests

```shell
make test            # pytest with doctests and coverage, in parallel
make test-nightly    # the full-size runs marked nightly
make mypy            # strict type checks
make ruff            # lint
make ci-unit         # what the forges run: a locked uv sync, then pytest
```

`make test` skips tests marked `nightly`. Without make, run `uv run pytest`.
`make ci-local` runs a CI workflow locally with act, Forgejo or Woodpecker; `make help`
lists its variants.

## The docker mixnet

The katzenpost docker mixnet is a whole network on one machine, with epochs of minutes
rather than hours. Its `kpclientd` listens on TCP `127.0.0.1:64331`.

```shell
make mixnet-up      # clone ./katzenpost if needed and start the mixnet
make mixnet-down    # stop it
```

## Two GUIs on one machine

Each instance is its own `KQT_STATE`, and both dial the mixnet's `kpclientd` through
`config/thinclient.docker.toml`:

```shell
make mixnet-up                  # clone ./katzenpost if needed, start it
make run-docker INSTANCE=alice  # one GUI, state file docker-alice.sqlite3
make run-docker INSTANCE=bob    # a second GUI, state file docker-bob.sqlite3
make mixnet-down
```

## Integration tests

```shell
make docker-integration    # against the docker mixnet
make check-live            # against any kpclientd already listening
```

`make docker-integration` installs katzenqt in editable mode, and also installs a local
[thin_client](https://github.com/katzenpost/thin_client) checkout from `~/thin_client`
(`THIN_CLIENT_DIR`) when one exists, so protocol changes not yet released are exercised.
It runs the tests in parallel (`KQT_INTEGRATION_PARALLEL`, default 4), then the
`serial_docker` tests, which bounce the shared containers, one at a time.

`make check-live` looks for a daemon on `KATZENQT_KPCLIENTD_HOST:KATZENQT_KPCLIENTD_PORT`
(default `127.0.0.1:64331`) and runs `tests/integration` against it.

## A remote desktop

`webtop/` holds a containerized desktop for working on katzenqt on a remote server
through a browser; see [webtop/README.md](../webtop/README.md).
