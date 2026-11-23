import pytest
from app import add, divide


def test_add() -> None:
    assert add(1, 2) == 3
    assert add(-1, 1) == 0


def test_divide() -> None:
    assert divide(6, 3) == 2.0
    with pytest.raises(ZeroDivisionError):
        divide(1, 0)
