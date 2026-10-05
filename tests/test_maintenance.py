"""车间计划保养核对算法单元测试（仅依赖标准库）。

可用 pytest 运行，也可直接：python3 tests/test_maintenance.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.attribution import total_minutes  # noqa: E402
from app.maintenance import (  # noqa: E402
    MaintenanceShiftView,
    verify_plan_maintenance,
)

UTC = timezone.utc
BASE = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def t(minute: float) -> datetime:
    return BASE + timedelta(minutes=minute)


def iv(a: float, b: float):
    return (BASE + timedelta(minutes=a), BASE + timedelta(minutes=b))


def shift(
    shift_id: int,
    coverage,
    maintenance,
    revision: int = 1,
    seq: int = 1,
) -> MaintenanceShiftView:
    return MaintenanceShiftView(
        shift_id=shift_id,
        revision=revision,
        seq=seq,
        coverage=coverage,
        maintenance_intervals=list(maintenance),
    )


def test_all_executed():
    # 计划窗口 10-20；班次覆盖 0-60，保养段 10-20 → 全部已执行
    r = verify_plan_maintenance(
        t(0), t(60), [iv(10, 20)], [shift(1, iv(0, 60), [iv(10, 20)])]
    )
    assert r.executed == [iv(10, 20)]
    assert r.executed_minutes == 10
    assert r.not_executed == []
    assert r.unplanned == []
    assert r.unverified == []


def test_window_minutes_classified_exactly_once():
    # 窗口 10-50；班次覆盖 0-60，保养 10-20 与 40-45；
    # 窗口其余 20-40,45-50 在覆盖内但非保养（故障/待判/未停机）→ 未执行
    r = verify_plan_maintenance(
        t(0),
        t(60),
        [iv(10, 50)],
        [shift(1, iv(0, 60), [iv(10, 20), iv(40, 45)])],
    )
    assert r.executed == [iv(10, 20), iv(40, 45)]
    assert r.executed_minutes == 15
    assert r.not_executed == [iv(20, 40), iv(45, 50)]
    assert r.not_executed_minutes == 25
    assert r.unverified == []
    # 窗口分钟恰被三类瓜分（这里没有未核实）
    assert (
        r.executed_minutes + r.not_executed_minutes + r.unverified_minutes == 40
    )


def test_unplanned_outside_windows():
    # 窗口 10-20；保养 10-20（已执行）+ 30-35（窗口外 → 计划外）
    r = verify_plan_maintenance(
        t(0),
        t(60),
        [iv(10, 20)],
        [shift(1, iv(0, 60), [iv(10, 20), iv(30, 35)])],
    )
    assert r.executed == [iv(10, 20)]
    assert r.unplanned == [iv(30, 35)]
    assert r.unplanned_minutes == 5
    assert r.not_executed == []


def test_unverified_when_not_covered_by_settled_shift():
    # 窗口 10-40；班次只覆盖 0-25（保养 10-20）：
    # 10-20 已执行，20-25 未执行，25-40 无已结算班次 → 未核实（不计未执行）
    r = verify_plan_maintenance(
        t(0), t(60), [iv(10, 40)], [shift(1, iv(0, 25), [iv(10, 20)])]
    )
    assert r.executed == [iv(10, 20)]
    assert r.not_executed == [iv(20, 25)]
    assert r.not_executed_minutes == 5
    assert r.unverified == [iv(25, 40)]
    assert r.unverified_minutes == 15
    assert r.settled_minutes == 25
    # 窗口恒等式：已执行 + 未执行 + 未核实 = 全部窗口
    assert (
        r.executed_minutes + r.not_executed_minutes + r.unverified_minutes == 30
    )


def test_maintenance_in_gap_between_shifts_is_unverified_within_window():
    # 两个相邻班次 0-20、20-40（半开相接）；保养段只在各班次内；
    # 窗口 0-50：覆盖 0-40，40-50 无班次 → 未核实；
    # 窗口内保养 = 已执行；窗口外但期间内的保养（50-55）→ 计划外
    r = verify_plan_maintenance(
        t(0),
        t(60),
        [iv(0, 50)],
        [
            shift(1, iv(0, 20), [iv(0, 10)], revision=1, seq=1),
            shift(2, iv(20, 40), [iv(20, 30)], revision=1, seq=2),
        ],
    )
    assert r.executed == [iv(0, 10), iv(20, 30)]
    assert r.executed_minutes == 20
    assert r.not_executed == [iv(10, 20), iv(30, 40)]
    assert r.unverified == [iv(40, 50)]
    assert r.unplanned == []


def test_touching_executed_pieces_merge_into_maximal_interval():
    # 两个相接班次各有保养 10-20 / 20-30；窗口 10-30 → 合并为一个最大连续段
    r = verify_plan_maintenance(
        t(0),
        t(60),
        [iv(10, 30)],
        [
            shift(1, iv(0, 20), [iv(10, 20)], revision=1, seq=1),
            shift(2, iv(20, 40), [iv(20, 30)], revision=3, seq=7),
        ],
    )
    assert r.executed == [iv(10, 30)]
    assert r.executed_minutes == 20
    assert r.not_executed == []


def test_multiple_non_overlapping_windows():
    # 窗口 10-20 与 40-50；覆盖 0-60；保养 15-45（横跨两个窗口与窗口间隙）
    r = verify_plan_maintenance(
        t(0),
        t(60),
        [iv(10, 20), iv(40, 50)],
        [shift(1, iv(0, 60), [iv(15, 45)])],
    )
    # 已执行：15-20 与 40-45；未执行：10-15 与 45-50；计划外：20-40
    assert r.executed == [iv(15, 20), iv(40, 45)]
    assert r.not_executed == [iv(10, 15), iv(45, 50)]
    assert r.unplanned == [iv(20, 40)]
    assert r.unverified == []
    assert r.executed_minutes == 10
    assert r.not_executed_minutes == 10
    assert r.unplanned_minutes == 20


def test_period_clipping_of_shift_and_segments():
    # 班次 30-90、保养 40-80 均伸出计划期间 0-60：
    # 窗口 10-60（与期间终点对齐）；覆盖裁剪为 30-60；保养裁剪为 40-60
    r = verify_plan_maintenance(
        t(0), t(60), [iv(10, 60)], [shift(1, iv(30, 90), [iv(40, 80)])]
    )
    assert r.settled_minutes == 30
    assert r.executed == [iv(40, 60)]
    assert r.not_executed == [iv(30, 40)]
    assert r.unverified == [iv(10, 30)]
    assert r.unplanned == []  # 保养 60-80 在期间外，不计计划外


def test_no_shifts_all_windows_unverified():
    r = verify_plan_maintenance(t(0), t(60), [iv(10, 20)], [])
    assert r.executed == []
    assert r.not_executed == []
    assert r.unplanned == []
    assert r.unverified == [iv(10, 20)]
    assert r.unverified_minutes == 10
    assert r.settled_minutes == 0


def test_partial_overlap_window_with_coverage():
    # 窗口 10-30；覆盖 20-40；保养 20-25 → 10-20 未核实，20-25 已执行，25-30 未执行
    r = verify_plan_maintenance(
        t(0), t(60), [iv(10, 30)], [shift(1, iv(20, 40), [iv(20, 25)])]
    )
    assert r.unverified == [iv(10, 20)]
    assert r.executed == [iv(20, 25)]
    assert r.not_executed == [iv(25, 30)]


def test_maintenance_identities_with_two_shifts():
    # 恒等式：已执行+未执行+未核实=窗口总分钟；已执行+计划外=期间内保养总分钟
    windows = [iv(5, 15), iv(30, 70)]
    shifts_ = [
        shift(1, iv(0, 40), [iv(10, 35)], revision=2, seq=4),
        shift(2, iv(40, 55), [iv(40, 50)], revision=1, seq=9),
    ]
    r = verify_plan_maintenance(t(0), t(90), windows, shifts_)
    window_minutes = total_minutes(windows)
    assert (
        r.executed_minutes + r.not_executed_minutes + r.unverified_minutes
        == window_minutes
    )
    maintenance_in_period = total_minutes([iv(10, 35), iv(40, 50)])
    assert r.executed_minutes + r.unplanned_minutes == maintenance_in_period
    # 具体分类：已执行 10-15/30-35/40-50=20；未执行 5-10/35-40/50-55=15；
    # 未核实 55-70=15；计划外 15-30=15
    assert r.executed_minutes == 20
    assert r.not_executed_minutes == 15
    assert r.unverified_minutes == 15
    assert r.unplanned_minutes == 15


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
