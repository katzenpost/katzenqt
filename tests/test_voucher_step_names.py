from __future__ import annotations

from katzenqt import persistent, voucher


def _described(field: str) -> str:
    info = persistent.PendingVoucher.model_fields[field]
    assert info.description is not None
    return info.description


def _step_constants() -> "dict[str, str]":
    return {
        name: value
        for name, value in vars(voucher).items()
        if name.startswith("STEP_") and isinstance(value, str)
    }


def test_the_described_steps_are_exactly_the_ones_that_exist() -> None:
    described = set(_described("step").split(";")[0].strip().split("/"))
    assert described == set(_step_constants().values())


def test_no_step_constant_goes_undescribed() -> None:
    assert set(_step_constants()) == {
        "STEP_MINTED", "STEP_AWAITING", "STEP_INDUCTING",
    }


def test_the_description_says_a_finished_handshake_keeps_no_row() -> None:
    assert "deleted" in _described("step")


def test_the_role_field_names_the_roles_the_code_writes() -> None:
    described = _described("role")
    for role in ("joiner", "inductor"):
        assert role in described
