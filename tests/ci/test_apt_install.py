from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAKE = shutil.which("make") or "make"


@dataclass(frozen=True)
class _Fake:
    calls: Path
    env: dict[str, str]

    def make(
        self, *args: str, target: str = "apt-install",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [MAKE, "-f", str(ROOT / "Makefile"), target, *args],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=15,
        )

    def recorded(self, name: str) -> list[str]:
        path = self.calls / name
        return (
            path.read_text(encoding="ascii").splitlines()
            if path.is_file()
            else []
        )


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[_Fake]:
    binary = tmp_path / "bin"
    binary.mkdir()
    for name in ("bash", "printf", "sh", "make"):
        source = shutil.which(name)
        assert source is not None
        (binary / name).symlink_to(source)
    (binary / "id").write_text(
        'printf "%s\\n" "$FAKE_UID"\n', encoding="ascii"
    )
    (binary / "apt").write_text(
        'printf "%s\\n" "$*" >> "$CALLS/apt"\n',
        encoding="ascii",
    )
    (binary / "sudo").write_text(
        'printf "%s\\n" "$*" >> "$CALLS/sudo"\n'
        'if [[ "$FAKE_SUDO" == ok ]]; then\n'
        '  if [[ "$1" == -v ]]; then exit 0; fi\n'
        '  exec "$@"\n'
        "fi\n"
        "exit 1\n",
        encoding="ascii",
    )
    for name in ("id", "apt", "sudo"):
        (binary / name).chmod(0o755)
    calls = tmp_path / "calls"
    calls.mkdir()
    env = {
        "PATH": str(binary),
        "CALLS": str(calls),
        "FAKE_UID": "1000",
        "FAKE_SUDO": "no",
        "HOME": str(tmp_path),
    }
    yield _Fake(calls, env)


def test_root_installs_without_sudo(fake: _Fake) -> None:
    fake.env["FAKE_UID"] = "0"
    result = fake.make("APT_PACKAGES=git make")
    assert result.returncode == 0, result.stderr
    assert fake.recorded("apt") == ["install -y git make"]
    assert fake.recorded("sudo") == []


def test_a_sudoer_installs_through_sudo(fake: _Fake) -> None:
    fake.env["FAKE_SUDO"] = "ok"
    result = fake.make("APT_PACKAGES=git make")
    assert result.returncode == 0, result.stderr
    assert fake.recorded("sudo") == ["-v", "apt install -y git make"]
    assert fake.recorded("apt") == ["install -y git make"]


def test_a_user_without_sudo_is_told_how_to_run_as_root(fake: _Fake) -> None:
    result = fake.make("APT_PACKAGES=git make")
    assert result.returncode != 0
    assert "su -c 'apt install -y git make'" in result.stderr
    assert fake.recorded("apt") == []


def test_the_debian_package_targets_go_through_the_helper() -> None:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert text.count("sudo apt") == 1
    assert text.count("$(MAKE) apt-install APT_PACKAGES=") == 2


def test_the_readme_offers_the_root_alternative() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "su -c 'apt install -y git make'" in text
    assert ".profile" not in text


def test_the_printed_advice_is_one_pasteable_line(fake: _Fake) -> None:
    result = fake.make(target="install-debian-packages")
    assert result.returncode != 0
    advice = [
        line
        for line in result.stderr.splitlines()
        if line.startswith("  su -c ")
    ]
    assert len(advice) == 1
    assert "\t" not in advice[0]
    assert "libfontconfig1" in advice[0]
