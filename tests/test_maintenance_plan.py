"""车间计划保养提交与核对端到端测试（需 PostgreSQL）。

覆盖：
- 提交：窗口在期间内/互不重叠校验、同设备期间不重叠、同单号同内容幂等
  （窗口换序、等价时区）、异内容冲突、未知设备/单号；
- 核对：按固定序号取相交已结算班次最后修订及其固化保养段，四类分钟
  （已执行/未执行/计划外/未核实）、最大连续区间、所用序号与修订依据、
  seq 越界、历史修订缺固化明细整次不可用、后续更正不改变固定序号结果。

运行方式：docker compose exec app pytest tests/test_maintenance_plan.py
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

TABLES = (
    "maintenance_plan_windows",
    "maintenance_plans",
    "manual_adjudications",
    "shift_result_segments",
    "shift_results",
    "event_versions",
    "event_sources",
    "shifts",
    "devices",
    "change_counter",
)

DEVICE = {"id": "CNC-01", "name": "加工中心1号"}
PLANS_PATH = "/devices/CNC-01/maintenance-plans"


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


def _shift(client, day: int, sh: int = 8, eh: int = 16, device="CNC-01") -> int:
    r = client.post(
        "/shifts",
        json={
            "device_id": device,
            "start": f"2026-10-{day:02d}T{sh:02d}:00:00+08:00",
            "end": f"2026-10-{day:02d}T{eh:02d}:00:00+08:00",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _event(client, source, version, day, sh, eh, reason) -> None:
    def _fmt(x):
        if isinstance(x, int):
            return f"{x:02d}:00:00"
        hh, mm = x
        return f"{hh:02d}:{mm:02d}:00"

    r = client.post(
        "/events",
        json={
            "device_id": "CNC-01",
            "source_id": source,
            "version": version,
            "start": f"2026-10-{day:02d}T{_fmt(sh)}+08:00",
            "end": f"2026-10-{day:02d}T{_fmt(eh)}+08:00",
            "reason": reason,
        },
    )
    assert r.status_code == 201, r.text


def _settle(client, shift_id) -> None:
    r = client.post(f"/shifts/{shift_id}/settle")
    assert r.status_code == 200, r.text


def _plan_body(
    plan_no="MP-001",
    start="2026-10-01T08:00:00+08:00",
    end="2026-10-01T16:00:00+08:00",
    windows=None,
    device="CNC-01",
):
    if windows is None:
        windows = [
            {
                "start": "2026-10-01T10:00:00+08:00",
                "end": "2026-10-01T11:00:00+08:00",
            }
        ]
    return {
        "device_id": device,
        "plan_no": plan_no,
        "start": start,
        "end": end,
        "windows": windows,
    }


def _submit(client, body=None, device="CNC-01"):
    return client.post(
        f"/devices/{device}/maintenance-plans", json=body or _plan_body()
    )


def _verify(client, plan_no, seq=None, device="CNC-01"):
    url = f"/maintenance-plans/{plan_no}/verification"
    if seq is not None:
        url += f"?seq={seq}"
    return client.get(url)


# ---------------------------------------------------------------- 提交校验


def test_submit_plan_creates_and_reads_back(client):
    client.post("/devices", json=DEVICE)
    body = _plan_body(
        windows=[
            {"start": "2026-10-01T10:00:00+08:00", "end": "2026-10-01T11:00:00+08:00"},
            {"start": "2026-10-01T13:00:00+08:00", "end": "2026-10-01T13:30:00+08:00"},
        ]
    )
    r = _submit(client, body)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["idempotent"] is False
    assert data["plan_no"] == "MP-001"
    assert data["start"] == "2026-10-01T00:00:00Z"
    assert [w["window_no"] for w in data["windows"]] == [1, 2]
    assert [w["minutes"] for w in data["windows"]] == [60, 30]

    # 按单号读回
    r2 = client.get("/maintenance-plans/MP-001")
    assert r2.status_code == 200
    assert r2.json()["id"] == data["id"]
    assert r2.json()["idempotent"] is True

    # 按设备列出
    r3 = client.get(PLANS_PATH)
    assert r3.status_code == 200
    assert [p["plan_no"] for p in r3.json()] == ["MP-001"]


def test_submit_idempotent_same_content_window_reorder_and_timezone(client):
    client.post("/devices", json=DEVICE)
    body = _plan_body(
        windows=[
            {"start": "2026-10-01T13:00:00+08:00", "end": "2026-10-01T14:00:00+08:00"},
            {"start": "2026-10-01T10:00:00+08:00", "end": "2026-10-01T11:00:00+08:00"},
        ]
    )
    r1 = _submit(client, body)
    assert r1.status_code == 201

    # 窗口换序 → 同内容幂等
    r2 = _submit(
        client,
        _plan_body(
            windows=[
                {"start": "2026-10-01T10:00:00+08:00", "end": "2026-10-01T11:00:00+08:00"},
                {"start": "2026-10-01T13:00:00+08:00", "end": "2026-10-01T14:00:00+08:00"},
            ]
        ),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["idempotent"] is True
    assert r2.json()["id"] == r1.json()["id"]
    # 读回窗口恒按 window_no 升序
    assert [w["start"] for w in r2.json()["windows"]] == [
        "2026-10-01T02:00:00Z",
        "2026-10-01T05:00:00Z",
    ]

    # 等价时区（同一时刻，UTC+00:00 表达）→ 仍同内容
    r3 = _submit(
        client,
        _plan_body(
            start="2026-09-30T16:00:00+00:00",
            end="2026-10-01T08:00:00+00:00",
            windows=[
                {"start": "2026-10-01T02:00:00+00:00", "end": "2026-10-01T03:00:00+00:00"},
                {"start": "2026-10-01T05:00:00+00:00", "end": "2026-10-01T06:00:00+00:00"},
            ],
        ),
    )
    assert r3.status_code == 200
    assert r3.json()["idempotent"] is True


def test_submit_same_no_different_content_conflict(client):
    client.post("/devices", json=DEVICE)
    assert _submit(client).status_code == 201
    r = _submit(
        client,
        _plan_body(
            windows=[
                {"start": "2026-10-01T10:00:00+08:00", "end": "2026-10-01T11:30:00+08:00"}
            ]
        ),
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "MAINTENANCE_PLAN_CONFLICT"


def test_submit_overlapping_period_same_device_409(client):
    client.post("/devices", json=DEVICE)
    assert _submit(client, _plan_body(plan_no="MP-A")).status_code == 201
    # 与 MP-A 的期间 08-16 重叠
    r = _submit(
        client,
        _plan_body(
            plan_no="MP-B",
            start="2026-10-01T15:00:00+08:00",
            end="2026-10-01T20:00:00+08:00",
        ),
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "MAINTENANCE_PLAN_OVERLAP"

    # 端点相接允许（16:00 起）
    r2 = _submit(
        client,
        _plan_body(
            plan_no="MP-C",
            start="2026-10-01T16:00:00+08:00",
            end="2026-10-01T20:00:00+08:00",
            windows=[
                {"start": "2026-10-01T16:00:00+08:00", "end": "2026-10-01T17:00:00+08:00"}
            ],
        ),
    )
    assert r2.status_code == 201, r2.text


def test_submit_window_out_of_period_or_overlapping_422(client):
    client.post("/devices", json=DEVICE)
    # 窗口越出期间
    r = _submit(
        client,
        _plan_body(
            windows=[
                {"start": "2026-10-01T07:30:00+08:00", "end": "2026-10-01T09:00:00+08:00"}
            ]
        ),
    )
    assert r.status_code == 422

    # 窗口互相重叠
    r2 = _submit(
        client,
        _plan_body(
            windows=[
                {"start": "2026-10-01T10:00:00+08:00", "end": "2026-10-01T11:00:00+08:00"},
                {"start": "2026-10-01T10:30:00+08:00", "end": "2026-10-01T12:00:00+08:00"},
            ]
        ),
    )
    assert r2.status_code == 422

    # 空窗口列表
    r3 = _submit(client, _plan_body(windows=[]))
    assert r3.status_code == 422

    # 缺时区 / 未对齐整分钟
    r4 = _submit(
        client,
        _plan_body(
            start="2026-10-01T08:00:00",
            end="2026-10-01T16:00:00+08:00",
        ),
    )
    assert r4.status_code == 422
    r5 = _submit(
        client,
        _plan_body(
            windows=[
                {"start": "2026-10-01T10:00:30+08:00", "end": "2026-10-01T11:00:00+08:00"}
            ]
        ),
    )
    assert r5.status_code == 422


def test_submit_unknown_device_404(client):
    r = _submit(client, _plan_body(device="NOPE"), device="NOPE")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


def test_submit_device_body_path_mismatch_422(client):
    client.post("/devices", json=DEVICE)
    r = client.post(
        "/devices/CNC-01/maintenance-plans", json=_plan_body(device="OTHER")
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------- 核对


def test_verify_four_categories(client):
    client.post("/devices", json=DEVICE)
    shift_id = _shift(client, 1)  # 08:00-16:00
    # 系统保养 09:30-10:30；故障 14:00-15:00
    _event(client, "MES-1", 1, 1, (9, 30), (10, 30), "maintenance")
    _event(client, "PLC-9", 1, 1, 14, 15, "failure")
    _settle(client, shift_id)  # seq=1

    # 计划期间 = 班次；窗口 10:00-11:00 与 13:30-14:30
    body = _plan_body(
        plan_no="MP-V1",
        windows=[
            {"start": "2026-10-01T10:00:00+08:00", "end": "2026-10-01T11:00:00+08:00"},
            {"start": "2026-10-01T13:30:00+08:00", "end": "2026-10-01T14:30:00+08:00"},
        ],
    )
    assert _submit(client, body).status_code == 201

    r = _verify(client, "MP-V1")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seq"] == 1
    assert data["settled_minutes"] == 480
    # 已执行：窗口与保养段交集 10:00-10:30
    assert data["executed"] == [
        {"start": "2026-10-01T02:00:00Z", "end": "2026-10-01T02:30:00Z", "minutes": 30}
    ]
    assert data["executed_minutes"] == 30
    # 未执行：窗口内已结算但非保养 = 10:30-11:00 + 13:30-14:30（故障段也算）
    assert data["not_executed"] == [
        {"start": "2026-10-01T02:30:00Z", "end": "2026-10-01T03:00:00Z", "minutes": 30},
        {"start": "2026-10-01T05:30:00Z", "end": "2026-10-01T06:30:00Z", "minutes": 60},
    ]
    assert data["not_executed_minutes"] == 90
    # 计划外：期间内窗口外的保养 09:30-10:00
    assert data["unplanned"] == [
        {"start": "2026-10-01T01:30:00Z", "end": "2026-10-01T02:00:00Z", "minutes": 30}
    ]
    assert data["unplanned_minutes"] == 30
    assert data["unverified"] == []
    assert data["unverified_minutes"] == 0
    # 修订依据
    assert data["shifts"] == [{"shift_id": shift_id, "revision": 1, "seq": 1}]
    # 窗口恒等式
    assert (
        data["executed_minutes"]
        + data["not_executed_minutes"]
        + data["unverified_minutes"]
        == 120
    )


def test_verify_unsettled_minutes_are_unverified_not_not_executed(client):
    client.post("/devices", json=DEVICE)
    _shift(client, 1, sh=8, eh=12)  # 仅 08:00-12:00 有班次（尚未结算）
    body = _plan_body(
        plan_no="MP-V2",
        start="2026-10-01T08:00:00+08:00",
        end="2026-10-01T16:00:00+08:00",
        windows=[
            {"start": "2026-10-01T10:00:00+08:00", "end": "2026-10-01T14:00:00+08:00"}
        ],
    )
    assert _submit(client, body).status_code == 201

    # 未结算班次不参与：窗口分钟全部未核实
    r = _verify(client, "MP-V2")
    assert r.status_code == 200
    data = r.json()
    assert data["shifts"] == []
    assert data["unverified"] == [
        {"start": "2026-10-01T02:00:00Z", "end": "2026-10-01T06:00:00Z", "minutes": 240}
    ]
    assert data["unverified_minutes"] == 240
    assert data["not_executed"] == []
    assert data["executed"] == []
    assert data["settled_minutes"] == 0

    # 结算后（无任何事件 → 无保养）：班次覆盖内 10-12 计未执行，12-14 仍未核实
    _settle(client, 1)
    r2 = _verify(client, "MP-V2")
    data2 = r2.json()
    assert data2["seq"] == 1
    assert data2["settled_minutes"] == 240
    assert data2["not_executed_minutes"] == 120
    assert data2["unverified_minutes"] == 120


def test_verify_fixed_seq_replay_after_correction(client):
    client.post("/devices", json=DEVICE)
    shift_id = _shift(client, 1)
    _event(client, "MES-1", 1, 1, 10, 11, "maintenance")
    _settle(client, shift_id)  # rev1 seq1：保养 10-11

    assert _submit(client, _plan_body(plan_no="MP-V3")).status_code == 201

    r1 = _verify(client, "MP-V3", seq=1)
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["executed_minutes"] == 60
    assert d1["not_executed_minutes"] == 0
    assert d1["shifts"] == [{"shift_id": shift_id, "revision": 1, "seq": 1}]

    # 迟到更正：保养缩短为 10:00-10:30 → rev2 seq2
    _event(client, "MES-1", 2, 1, 10, (10, 30), "maintenance")

    # 缺省 seq 固定最新：已执行 30、未执行 30
    r_latest = _verify(client, "MP-V3")
    assert r_latest.json()["seq"] == 2
    assert r_latest.json()["executed_minutes"] == 30
    assert r_latest.json()["not_executed_minutes"] == 30
    assert r_latest.json()["shifts"] == [{"shift_id": shift_id, "revision": 2, "seq": 2}]

    # 固定 seq=1 重放：结果不变（不以当前状态推算）
    r_old = _verify(client, "MP-V3", seq=1)
    d_old = r_old.json()
    assert d_old["seq"] == 1
    assert d_old["executed_minutes"] == 60
    assert d_old["not_executed_minutes"] == 0
    assert d_old["shifts"] == [{"shift_id": shift_id, "revision": 1, "seq": 1}]


def test_verify_seq_beyond_latest_422(client):
    client.post("/devices", json=DEVICE)
    _shift(client, 1)
    _settle(client, 1)
    assert _submit(client, _plan_body(plan_no="MP-V4")).status_code == 201
    r = _verify(client, "MP-V4", seq=999999)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_SEQ"


def test_verify_seq_zero_before_any_settlement(client):
    client.post("/devices", json=DEVICE)
    _shift(client, 1)  # 未结算
    assert _submit(client, _plan_body(plan_no="MP-V5")).status_code == 201
    r = _verify(client, "MP-V5", seq=0)
    assert r.status_code == 200
    data = r.json()
    assert data["seq"] == 0
    assert data["shifts"] == []
    assert data["unverified_minutes"] == 60  # 窗口 10-11 全无结算覆盖
    assert data["executed_minutes"] == 0


def test_verify_unknown_plan_404(client):
    r = _verify(client, "NOPE")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "MAINTENANCE_PLAN_NOT_FOUND"


def test_verify_segments_unavailable_fails_whole_query(client):
    client.post("/devices", json=DEVICE)
    shift_id = _shift(client, 1)
    _event(client, "MES-1", 1, 1, 10, 11, "maintenance")
    _settle(client, shift_id)
    assert _submit(client, _plan_body(plan_no="MP-V6")).status_code == 201

    # 结构引入前的历史修订：整次核对报不可用，不以现态推算
    with engine.begin() as conn:
        conn.execute(text("UPDATE shift_results SET segments_available = FALSE"))

    r = _verify(client, "MP-V6")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "SEGMENTS_UNAVAILABLE"


def test_verify_uses_last_revision_per_shift_at_seq(client):
    # seq=2 时班次有 rev1(seq1) 与 rev2(seq2)，核对必须取 rev2
    client.post("/devices", json=DEVICE)
    shift_id = _shift(client, 1)
    _event(client, "MES-1", 1, 1, 10, 11, "maintenance")
    _settle(client, shift_id)  # rev1 seq1
    # 裁定 10:00-11:00 为故障 → rev2 seq2（保养分钟消失）
    r = client.post(
        f"/shifts/{shift_id}/adjudications",
        json={
            "ruling_no": "R-1",
            "start": "2026-10-01T10:00:00+08:00",
            "end": "2026-10-01T11:00:00+08:00",
            "reason": "failure",
            "note": "改判",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["revision"] == 2

    assert _submit(client, _plan_body(plan_no="MP-V7")).status_code == 201

    latest = _verify(client, "MP-V7").json()
    assert latest["shifts"] == [{"shift_id": shift_id, "revision": 2, "seq": 2}]
    assert latest["executed_minutes"] == 0
    assert latest["not_executed_minutes"] == 60

    at1 = _verify(client, "MP-V7", seq=1).json()
    assert at1["shifts"] == [{"shift_id": shift_id, "revision": 1, "seq": 1}]
    assert at1["executed_minutes"] == 60


def test_list_unknown_device_404(client):
    r = client.get("/devices/NOPE/maintenance-plans")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


def test_negative_seq_422(client):
    client.post("/devices", json=DEVICE)
    assert _submit(client, _plan_body(plan_no="MP-V8")).status_code == 201
    r = _verify(client, "MP-V8", seq=-1)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_touching_periods_and_windows_allowed(client):
    client.post("/devices", json=DEVICE)
    body = _plan_body(
        plan_no="MP-T1",
        windows=[
            {"start": "2026-10-01T10:00:00+08:00", "end": "2026-10-01T11:00:00+08:00"},
            {"start": "2026-10-01T11:00:00+08:00", "end": "2026-10-01T12:00:00+08:00"},
        ],
    )
    assert _submit(client, body).status_code == 201
    # 相接计划期间（16:00 起）允许
    body2 = _plan_body(
        plan_no="MP-T2",
        start="2026-10-01T16:00:00+08:00",
        end="2026-10-01T20:00:00+08:00",
        windows=[
            {"start": "2026-10-01T16:00:00+08:00", "end": "2026-10-01T17:00:00+08:00"}
        ],
    )
    assert _submit(client, body2).status_code == 201
    # 两个窗口相接，读回各自独立但核对时合并为最大连续区间
    shift_id = _shift(client, 1)
    _event(client, "MES-1", 1, 1, 10, 12, "maintenance")
    _settle(client, shift_id)
    r = _verify(client, "MP-T1")
    assert r.status_code == 200, r.text
    data = r.json()
    # 10:00-12:00 保养跨两个相接窗口 → 单个最大连续已执行区间（120 分钟）
    assert data["executed"] == [
        {"start": "2026-10-01T02:00:00Z", "end": "2026-10-01T04:00:00Z", "minutes": 120}
    ]
    assert data["executed_minutes"] == 120
    assert data["not_executed_minutes"] == 0
    assert data["unverified_minutes"] == 0
