"""车间按设备核对计划保养是否执行（整分钟半开区间，纯标准库）。

输入
- 计划期间 [period_start, period_end)（带时区整分钟、半开）与计划保养窗口
  windows：窗口非空、互不重叠（端点相接允许）且必须完全落在期间内（可与期间
  端点对齐）；
- 与计划期间半开相交的已结算班次：每班给出在固定变更序号下所选最后修订的
  固化归因明细，本模块只读取其中**保养（maintenance）段**（区间调用前已可
  以是整班次范围；内部统一裁剪到计划期间）。

核对语义（逐分钟，只在半开区间上运算）
- **已执行 executed**：计划窗口与"保养"段的交集；
- **未执行 not_executed**：计划窗口内落在已结算班次覆盖中、但不是保养的
  分钟（覆盖内的故障/待判/未停机分钟）；
- **计划外 unplanned**：计划期间内、窗口之外的"保养"段；
- **未核实 unverified**：计划窗口中未被任何已结算班次覆盖的分钟（没有结算
  修订可依据），单列，**不计未执行**。

恒等关系（窗口 ∩ 期间 = 全部窗口，窗口已要求在期间内）：
  executed ∪ not_executed ∪ unverified = 全部窗口（三者互不相交）；
  executed ∪ unplanned = 期间内保养段（按窗口划分）。
所有输出区间均为按时间顺序的最大连续半开区间。

本模块只依赖标准库，可脱离 Web/数据库独立测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .attribution import Interval, intersect, merge, subtract, total_minutes

#: 已执行（计划窗口 ∩ 保养段）
EXECUTED = "executed"
#: 未执行（窗口内已结算但非保养分钟）
NOT_EXECUTED = "not_executed"
#: 计划外（期间内窗口外的保养段）
UNPLANNED = "unplanned"
#: 未核实（窗口内未被已结算班次覆盖的分钟）
UNVERIFIED = "unverified"


@dataclass(frozen=True)
class MaintenanceShiftView:
    """一个与计划期间相交的已结算班次（固定序号下所选最后修订）。

    coverage 为班次半开区间；maintenance_intervals 为该修订固化明细中的
    保养段。调用前无需预先裁剪，本模块统一裁剪到计划期间。
    """

    shift_id: int
    revision: int
    seq: int
    coverage: Interval
    maintenance_intervals: list[Interval]


@dataclass(frozen=True)
class MaintenanceVerification:
    """核对结果：四类最大连续区间（均按时间顺序）及各自分钟总数。"""

    executed: list[Interval] = field(default_factory=list)
    not_executed: list[Interval] = field(default_factory=list)
    unplanned: list[Interval] = field(default_factory=list)
    unverified: list[Interval] = field(default_factory=list)
    executed_minutes: int = 0
    not_executed_minutes: int = 0
    unplanned_minutes: int = 0
    unverified_minutes: int = 0
    #: 期间内已结算班次覆盖总分钟（核对"已执行/未执行"的结算依据范围）
    settled_minutes: int = 0


def _intersection_union(
    a: list[Interval], b: list[Interval]
) -> list[Interval]:
    """两组半开区间并集的交集（逐对求交后合并）。"""
    pieces: list[Interval] = []
    for x in a:
        for y in b:
            iv = intersect(x, y)
            if iv is not None:
                pieces.append(iv)
    return merge(pieces)


def verify_plan_maintenance(
    period_start: datetime,
    period_end: datetime,
    windows: list[Interval],
    shifts: list[MaintenanceShiftView],
) -> MaintenanceVerification:
    """在计划期间内核对计划保养窗口是否已按固化归因明细执行。

    windows 必须已通过"非空、落在期间内、互不重叠"校验；shifts 为与计划
    期间半开相交、在固定序号下已结算的班次（各班次所选最后修订）。
    所有输入均须已对齐整分钟（时区由调用方归一，运算只比较时刻）。
    """
    period = (period_start, period_end)

    # 计划窗口（归一裁剪到期间，合并相接；窗口已要求落在期间内且互不重叠）
    window_clipped: list[Interval] = []
    for s, e in windows:
        iv = intersect((s, e), period)
        if iv is not None:
            window_clipped.append(iv)
    window_union = merge(window_clipped)

    # 固化保养段并集 / 已结算班次覆盖并集（均裁剪到计划期间）
    maintenance_raw: list[Interval] = []
    coverage_raw: list[Interval] = []
    for sh in shifts:
        for s, e in sh.maintenance_intervals:
            iv = intersect((s, e), period)
            if iv is not None:
                maintenance_raw.append(iv)
        iv = intersect(sh.coverage, period)
        if iv is not None:
            coverage_raw.append(iv)
    maintenance_union = merge(maintenance_raw)
    coverage_union = merge(coverage_raw)

    # 已执行 = 窗口 ∩ 保养段
    executed = _intersection_union(window_union, maintenance_union)

    # 已结算覆盖内的窗口分钟：已执行之外即未执行（故障/待判/未停机均算）
    windows_in_coverage = _intersection_union(window_union, coverage_union)
    not_executed = subtract(windows_in_coverage, executed)

    # 计划外 = 期间内窗口之外的保养段
    unplanned = subtract(maintenance_union, window_union)

    # 未核实 = 窗口中未被已结算班次覆盖的分钟（单列，不计未执行）
    unverified = subtract(window_union, coverage_union)

    return MaintenanceVerification(
        executed=executed,
        not_executed=not_executed,
        unplanned=unplanned,
        unverified=unverified,
        executed_minutes=total_minutes(executed),
        not_executed_minutes=total_minutes(not_executed),
        unplanned_minutes=total_minutes(unplanned),
        unverified_minutes=total_minutes(unverified),
        settled_minutes=total_minutes(coverage_union),
    )
