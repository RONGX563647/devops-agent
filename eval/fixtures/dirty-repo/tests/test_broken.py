"""dirty-repo 的测试：test_average_empty 必然失败。"""

from calc import add, average


def test_add() -> None:
    assert add(1, 2) == 3


def test_average() -> None:
    assert average([2, 4, 6]) == 4.0


def test_average_empty() -> None:
    """这一条会失败：average([]) 抛 ZeroDivisionError 而不是返回 0.0。"""
    assert average([]) == 0.0
