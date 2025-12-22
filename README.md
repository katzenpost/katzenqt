So you want to try the `katzenqt` program? These instructions should help
developers to start to run the software.

To use the new `Makefile` targets please ensure that you have `git` and `make`
installed before proceeding:
```
  sudo apt install -y git make
```

First, git clone the `katzenqt` repo and `cd` into `katzenqt`:
```
  git clone https://www.github.com/katzenpost/katzenqt ~/katzenqt
  cd ~/katzenqt/
```

At this point your system should have all of the required tools to proceed.
Your user account should have `~/.local/katzenpost/client2.toml` and
`~/.local/katzenpost/thinclient.toml` ready for software that expects to find
the configuration files in those locations.

Run these two commands on Debian GNU/Linux to setup the system, the environment
and to run `katzenqt`:
```
make deps
make run
```

Or you could run a series of make targets inside `katzenqt` if the previous
commands did not result in `katzenqt` displaying a user interface:
```
  make system-setup
  make setup-uv
  make setup
  make test
  make kpclientd
  make install-kpclient
  make kpclientd.service
  make status
```

If the `make status` shows the following then things are probably working:
```
backend: uv
venv: .venv
kpclientd(bin): /home/user/.local/bin/kpclientd
kpclientd(service): active
kpclientd(path): found
```

It should then be possible to run `katzenqt` using your configured and prepared
virtual environment as shown by the `make status` command. There are several
ways to run `katzenqt` and one that is expected to work at this point is `make
run`:
```
  make run
```

These instructions are temporary and later the user story should include a
single command that will be well documented.
