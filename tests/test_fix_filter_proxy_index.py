from __future__ import annotations

import operator
import os
from typing import TYPE_CHECKING, cast

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from katzenqt.qt_models import FilterProxyModel  # noqa: E402

if TYPE_CHECKING:
    from katzenqt.katzen import MainWindow


@pytest.fixture(scope="module", autouse=True)
def _qt_app() -> QApplication:
    return cast(QApplication, QApplication.instance() or QApplication([]))


def test_filter_proxy_model_is_not_pretending_to_be_an_integer() -> None:
    model = FilterProxyModel(cast("MainWindow", QMainWindow()))
    with pytest.raises(TypeError):
        operator.index(model)  # type: ignore[arg-type]
