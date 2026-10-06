"""端到端 API 测试：待付额度预约（付款预约）。

覆盖：
- 创建预约（占额/可预约额负值按零/超额拒绝/同号同内容重放/异内容冲突）；
- 直接付款扣除待执行预约占额（避免绕过预约）；
- 执行（同事务校验批次与余额、登记现有付款并解除占额、重复执行返回原付款、
  换付款编号冲突、付款编号被直接付款占用、已取消/已失效不可执行）；
- 取消释放占额（已执行/已取消/已失效状态迁移）；
- 新关账使旧批次待执行预约失效并释放占额；已执行付款及后续退票口径不变；
- 作者/批次不存在、过期批次、金额非法（422）等错误；
- 作者结算总览含占额/可用额与预约记录；并发预约 + 执行不超额。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import db, main


@pytest.fixture()
def client():
    db.open_database(":memory:")
    db.init_db()
    with TestClient(main.app) as c:
        yield c


def setup_author(client, author="A1", royalty_cents=100000):
    """录入作品 + 销售（单作者 100%）+ 关账一批，使作者应计为 royalty_cents。"""
    client.post(
        "/works",
        json={
            "work_id": "W1",
            "title": "预约测试作品",
            "tiers": [{"threshold": 0, "rate": 0.1}],
            "authors": [{"author_id": author, "ratio": 100}],
        },
    )
    # 1000 册 × 10.00 元 × 10% = 1.00 元/册 → 100000 分应计。
    client.post(
        "/sales",
        json={
            "sale_id": "S1",
            "work_id": "W1",
            "business_time": "2026-09-01T10:00:00",
            "copies": 1000,
            "net_price": "10.00",
        },
    )
    r = client.post("/closes", json={"request_id": "C1"})
    assert r.status_code == 201
    close_id = r.json()["close_id"]
    s = client.get(f"/authors/{author}/settlement").json()
    assert s["accrued_cents"] == royalty_cents
    return close_id


def reserve(client, rid, amount, author="A1", close_id=1):
    return client.post(
        "/payment-reservations",
        json={
            "reservation_id": rid,
            "author_id": author,
            "close_id": close_id,
            "amount_cents": amount,
        },
    )


# ---------------------------------------------------------------- 创建与占额


def test_reserve_happy_path_fields(client):
    close_id = setup_author(client)
    r = reserve(client, "R1", 30000, close_id=close_id)
    assert r.status_code == 201
    b = r.json()
    assert b["reservation_id"] == "R1"
    assert b["status"] == "pending" and b["status_label"] == "待执行"
    assert b["amount_cents"] == 30000 and b["close_id"] == close_id
    assert b["payment_id"] is None and b["payment"] is None
    assert b["expired_at_close_id"] is None
    assert b["accrued_cents"] == 100000
    assert b["paid_before_cents"] == 0
    assert b["reserved_before_cents"] == 0
    assert b["available_cents"] == 100000
    # 作者当前可用额已扣本预约占额
    assert b["latest_close_id"] == close_id
    assert b["current_reserved_cents"] == 30000
    assert b["author_available_cents"] == 70000
    assert b["executed_at"] is None and b["cancelled_at"] is None and b["expired_at"] is None


def test_reserve_reduces_available_for_following_reservation(client):
    close_id = setup_author(client)
    assert reserve(client, "R1", 60000, close_id=close_id).status_code == 201
    r = reserve(client, "R2", 30000, close_id=close_id)
    assert r.status_code == 201
    b = r.json()
    assert b["reserved_before_cents"] == 60000
    assert b["available_cents"] == 40000  # 创建前可预约额
    assert b["author_available_cents"] == 10000  # 创建后剩余


def test_reserve_over_quota_rejected(client):
    close_id = setup_author(client)
    assert reserve(client, "R1", 60000, close_id=close_id).status_code == 201
    r = reserve(client, "R2", 40001, close_id=close_id)
    assert r.status_code == 400
    assert "可预约额" in r.json()["detail"]
    # 被拒绝的预约不落库
    assert client.get("/payment-reservations/R2").status_code == 404


def test_reserve_full_amount_then_none_left(client):
    close_id = setup_author(client)
    assert reserve(client, "R1", 100000, close_id=close_id).status_code == 201
    # 可预约额为 0，任何正金额都被拒绝（负值按零计）
    r = reserve(client, "R2", 1, close_id=close_id)
    assert r.status_code == 400


def test_reserve_direct_payment_must_deduct_hold(client):
    """现有直接付款也须扣除待执行预约占额，避免绕过预约。"""
    close_id = setup_author(client)
    assert reserve(client, "R1", 60000, close_id=close_id).status_code == 201
    # 直接付款只可使用未被预约的 40000
    ok = client.post(
        "/payments",
        json={"payment_id": "P1", "author_id": "A1", "close_id": close_id, "amount_cents": 40000},
    )
    assert ok.status_code == 201
    over = client.post(
        "/payments",
        json={"payment_id": "P2", "author_id": "A1", "close_id": close_id, "amount_cents": 1},
    )
    assert over.status_code == 400
    s = client.get("/authors/A1/settlement").json()
    assert s["paid_cents"] == 40000
    assert s["reserved_cents"] == 60000
    assert s["available_cents"] == 0 and s["payable_cents"] == 0


def test_reserve_idempotent_replay_and_conflict(client):
    close_id = setup_author(client)
    assert reserve(client, "R1", 30000, close_id=close_id).status_code == 201
    # 同号同内容重试 → 200 原状态
    r = reserve(client, "R1", 30000, close_id=close_id)
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    # 同号异内容 → 409
    r = reserve(client, "R1", 30001, close_id=close_id)
    assert r.status_code == 409
    r = reserve(client, "R1", 30000, author="A2", close_id=close_id)
    assert r.status_code == 409  # 异作者也算异内容


def test_reserve_validation_and_missing(client):
    close_id = setup_author(client)
    # 金额非正 → 422
    r = reserve(client, "R0", 0, close_id=close_id)
    assert r.status_code == 422
    r = reserve(client, "R0", -1, close_id=close_id)
    assert r.status_code == 422
    # 作者不存在 → 404
    r = reserve(client, "RX", 10, author="NOBODY", close_id=close_id)
    assert r.status_code == 404
    # 批次不存在 → 404
    r = reserve(client, "RY", 10, close_id=999)
    assert r.status_code == 404
    # 空预约号 → 422
    r = client.post(
        "/payment-reservations",
        json={"reservation_id": "", "author_id": "A1", "close_id": close_id, "amount_cents": 10},
    )
    assert r.status_code == 422


def test_reserve_on_expired_batch_rejected(client):
    close_id = setup_author(client)
    client.post("/closes", json={"request_id": "C2"})  # 空批
    r = reserve(client, "R1", 10, close_id=close_id)
    assert r.status_code == 409
    assert "最新批次" in r.json()["detail"]


# ---------------------------------------------------------------- 执行


def test_execute_reservation_registers_payment_and_releases_hold(client):
    close_id = setup_author(client)
    assert reserve(client, "R1", 60000, close_id=close_id).status_code == 201
    r = client.post("/payment-reservations/R1/execute", json={"payment_id": "PAY-1"})
    assert r.status_code == 201
    b = r.json()
    assert b["status"] == "executed" and b["status_label"] == "已执行"
    assert b["payment_id"] == "PAY-1"
    assert b["payment"] is not None
    assert b["payment"]["payment_id"] == "PAY-1"
    assert b["payment"]["amount_cents"] == 60000
    assert b["payment"]["close_id"] == close_id
    assert b["executed_at"]
    # 占额解除：60000 转为有效已付，剩余可用 40000
    assert b["current_paid_cents"] == 60000
    assert b["current_reserved_cents"] == 0
    assert b["author_available_cents"] == 40000
    # 现有付款查询可见，与直接付款同表同口径
    p = client.get("/payments/PAY-1")
    assert p.status_code == 200
    assert p.json()["amount_cents"] == 60000
    s = client.get("/authors/A1/settlement").json()
    assert s["paid_cents"] == 60000 and s["reserved_cents"] == 0
    assert s["available_cents"] == 40000


def test_execute_reservation_repeat_returns_original_payment(client):
    close_id = setup_author(client)
    reserve(client, "R1", 1000, close_id=close_id)
    r1 = client.post("/payment-reservations/R1/execute", json={"payment_id": "PAY-1"})
    assert r1.status_code == 201
    # 重复执行（同付款编号）→ 200 返回原付款
    r2 = client.post("/payment-reservations/R1/execute", json={"payment_id": "PAY-1"})
    assert r2.status_code == 200
    assert r2.json()["payment_id"] == "PAY-1"
    # 付款表只有一笔
    s = client.get("/authors/A1/settlement").json()
    assert [p["payment_id"] for p in s["payments"]] == ["PAY-1"]


def test_execute_reservation_switch_payment_id_conflict(client):
    close_id = setup_author(client)
    reserve(client, "R1", 1000, close_id=close_id)
    assert client.post("/payment-reservations/R1/execute", json={"payment_id": "PAY-1"}).status_code == 201
    # 换付款编号 → 409
    r = client.post("/payment-reservations/R1/execute", json={"payment_id": "PAY-2"})
    assert r.status_code == 409


def test_execute_reservation_payment_id_globally_unique(client):
    close_id = setup_author(client)
    reserve(client, "R1", 1000, close_id=close_id)
    reserve(client, "R2", 1000, close_id=close_id)
    assert client.post("/payment-reservations/R1/execute", json={"payment_id": "PAY-1"}).status_code == 201
    # R2 使用已被占用的付款编号 → 409
    r = client.post("/payment-reservations/R2/execute", json={"payment_id": "PAY-1"})
    assert r.status_code == 409
    # 预约仍待执行，换号可执行
    assert client.post("/payment-reservations/R2/execute", json={"payment_id": "PAY-2"}).status_code == 201


def test_execute_reservation_payment_id_used_by_direct_payment(client):
    close_id = setup_author(client)
    reserve(client, "R1", 1000, close_id=close_id)
    # 直接付款先用掉 PAY-1（占用剩余额度）
    assert client.post(
        "/payments",
        json={"payment_id": "PAY-1", "author_id": "A1", "close_id": close_id, "amount_cents": 1000},
    ).status_code == 201
    r = client.post("/payment-reservations/R1/execute", json={"payment_id": "PAY-1"})
    assert r.status_code == 409


def test_execute_reservation_balance_recheck_inside_tx(client):
    """执行时同事务复核余额：其他预约先执行后，本预约超额则拒绝。"""
    close_id = setup_author(client)
    reserve(client, "R1", 60000, close_id=close_id)
    reserve(client, "R2", 40000, close_id=close_id)
    assert client.post("/payment-reservations/R1/execute", json={"payment_id": "PAY-1"}).status_code == 201
    # R1 执行后有效已付 60000，R2 的 40000 恰好用尽 → 成功
    assert client.post("/payment-reservations/R2/execute", json={"payment_id": "PAY-2"}).status_code == 201


def test_execute_reservation_rejected_when_balance_consumed(client):
    """执行时同事务复核的实际价值：并发交错下预约创建后、执行前，其余额可能已不敷。

    构造占额规则下的交错（借助退票恢复可付制造"同额双预约"窗口）：
    1) R1=50000 创建并执行 → 已付 50000；
    2) R2=50000 创建 → 已执行+待执行合计恰 100000；
    3) R1 付款被退票 → 有效已付回 0，但 R2 仍占 50000；
    4) R3=50000 此时可创建（退票恢复了可付，R2+R3 占额 100000）；
    5) 直接付款无法越过 100000 占额；R2 先执行（已付 50000），R3 再执行仍恰好 50000。
    占额规则本身保证单调安全；本测试固定该不变量（任一时刻 已付 + pending占额 ≤ 应计）。
    """
    close_id = setup_author(client)
    reserve(client, "R1", 50000, close_id=close_id)
    reserve(client, "R2", 50000, close_id=close_id)
    assert client.post("/payment-reservations/R1/execute", json={"payment_id": "P1"}).status_code == 201
    assert client.post("/payment-reservations/R2/execute", json={"payment_id": "P2"}).status_code == 201
    assert client.post(
        "/payment-reversals", json={"reversal_id": "RV1", "payment_id": "P1"}
    ).status_code == 201
    s = client.get("/authors/A1/settlement").json()
    # 退票后：有效已付 50000（P2），无 pending 占额，可用恢复 50000
    assert s["paid_cents"] == 50000 and s["reserved_cents"] == 0
    assert s["available_cents"] == 50000


def test_paid_plus_pending_never_exceeds_accrued_under_mix(client):
    """混合交错不变量：任一笔成功的直接付款/预约/执行后，有效已付 + 待执行占额 ≤ 应计。"""
    close_id = setup_author(client)
    reserve(client, "R1", 30000, close_id=close_id)
    # 直接付款用未占额 70000 中的 20000
    assert client.post(
        "/payments",
        json={"payment_id": "PD1", "author_id": "A1", "close_id": close_id, "amount_cents": 20000},
    ).status_code == 201
    reserve(client, "R2", 40000, close_id=close_id)  # 已付20000 + 占30000 → 可约50000
    reserve(client, "R3", 10000, close_id=close_id)  # 再占 10000，已付+占=100000
    assert reserve(client, "R4", 1, close_id=close_id).status_code == 400  # 超出 1 分
    assert client.post(
        "/payments",
        json={"payment_id": "PD2", "author_id": "A1", "close_id": close_id, "amount_cents": 1},
    ).status_code == 400  # 直接付款同样无余额
    # 执行 R2（40000）：已付变 60000，pending 剩 R1+R3=40000
    assert client.post("/payment-reservations/R2/execute", json={"payment_id": "P2"}).status_code == 201
    s = client.get("/authors/A1/settlement").json()
    assert s["paid_cents"] + s["reserved_cents"] == 100000
    assert s["available_cents"] == 0


def test_execute_reservation_insufficient_balance_after_direct_payment(client):
    close_id = setup_author(client)
    reserve(client, "R1", 60000, close_id=close_id)
    # 直接付款用掉 50000（未占额部分 40000 + …）——只能付 40000
    assert client.post(
        "/payments",
        json={"payment_id": "P1", "author_id": "A1", "close_id": close_id, "amount_cents": 40000},
    ).status_code == 201
    # 此时执行 R1：排除自身后可用 = 100000 − 40000 = 60000，恰好够
    assert client.post("/payment-reservations/R1/execute", json={"payment_id": "PAY-R1"}).status_code == 201


def test_execute_after_reversal_then_other_reservation_still_payable(client):
    """执行后的付款可按现有口径退票；另一笔待执行预约仍按余额复核执行。"""
    close_id = setup_author(client)
    reserve(client, "R1", 50000, close_id=close_id)
    reserve(client, "R2", 50000, close_id=close_id)
    assert client.post("/payment-reservations/R1/execute", json={"payment_id": "PAY-1"}).status_code == 201
    # R1 付款被退票：有效已付回到 0，但 R1 已执行不可反向；R2 仍可执行 50000
    rev = client.post(
        "/payment-reversals",
        json={"reversal_id": "REV-1", "payment_id": "PAY-1"},
    )
    assert rev.status_code == 201
    assert client.post("/payment-reservations/R2/execute", json={"payment_id": "PAY-2"}).status_code == 201


def test_execute_cancelled_or_expired_reservation_rejected(client):
    close_id = setup_author(client)
    reserve(client, "R1", 1000, close_id=close_id)
    assert client.post("/payment-reservations/R1/cancel").status_code == 200
    r = client.post("/payment-reservations/R1/execute", json={"payment_id": "PAY-1"})
    assert r.status_code == 409  # 已取消不得反向转换

    reserve(client, "R2", 1000, close_id=close_id)
    client.post("/closes", json={"request_id": "C2"})
    r = client.post("/payment-reservations/R2/execute", json={"payment_id": "PAY-2"})
    assert r.status_code == 409  # 过期批次
    assert client.get("/payment-reservations/R2").json()["status"] == "expired"


def test_execute_missing_reservation_404(client):
    r = client.post("/payment-reservations/NOPE/execute", json={"payment_id": "P1"})
    assert r.status_code == 404


# ---------------------------------------------------------------- 取消


def test_cancel_reservation_releases_hold(client):
    close_id = setup_author(client)
    reserve(client, "R1", 60000, close_id=close_id)
    r = client.post("/payment-reservations/R1/cancel")
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "cancelled" and b["status_label"] == "已取消"
    assert b["cancelled_at"]
    assert b["current_reserved_cents"] == 0
    assert b["author_available_cents"] == 100000  # 占额释放
    # 释放后直接付款可全额使用
    ok = client.post(
        "/payments",
        json={"payment_id": "P1", "author_id": "A1", "close_id": close_id, "amount_cents": 100000},
    )
    assert ok.status_code == 201


def test_cancel_is_idempotent_and_illegal_transitions(client):
    close_id = setup_author(client)
    reserve(client, "R1", 1000, close_id=close_id)
    assert client.post("/payment-reservations/R1/cancel").status_code == 200
    # 重复取消幂等返回原状态
    r = client.post("/payment-reservations/R1/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"

    reserve(client, "R2", 1000, close_id=close_id)
    client.post("/payment-reservations/R2/execute", json={"payment_id": "PAY-2"})
    r = client.post("/payment-reservations/R2/cancel")
    assert r.status_code == 409  # 已执行不得取消

    r = client.post("/payment-reservations/NOPE/cancel")
    assert r.status_code == 404


def test_cancel_expired_reservation_rejected(client):
    close_id = setup_author(client)
    reserve(client, "R1", 1000, close_id=close_id)
    client.post("/closes", json={"request_id": "C2"})
    r = client.post("/payment-reservations/R1/cancel")
    assert r.status_code == 409


# ---------------------------------------------------------------- 关账失效


def test_new_close_expires_pending_reservations_and_releases_hold(client):
    close_id = setup_author(client)
    reserve(client, "R1", 60000, close_id=close_id)
    reserve(client, "R2", 30000, close_id=close_id)
    r = client.post("/closes", json={"request_id": "C2"})
    assert r.status_code == 201
    body = r.json()
    assert body["expired_reservations_count"] == 2

    r1 = client.get("/payment-reservations/R1").json()
    assert r1["status"] == "expired" and r1["status_label"] == "已失效"
    assert r1["expired_at_close_id"] == body["close_id"]
    assert r1["expired_at"]
    # 占额释放：新批次作者应计仍为 100000（空批净额 0），可用恢复
    assert r1["current_reserved_cents"] == 0
    assert r1["author_available_cents"] == 100000

    s = client.get("/authors/A1/settlement").json()
    assert s["reserved_cents"] == 0 and s["available_cents"] == 100000
    # 失效预约不可执行/取消
    assert client.post("/payment-reservations/R1/execute", json={"payment_id": "P1"}).status_code == 409
    assert client.post("/payment-reservations/R1/cancel").status_code == 409


def test_close_idempotent_replay_keeps_expiration_count(client):
    close_id = setup_author(client)
    reserve(client, "R1", 1000, close_id=close_id)
    r1 = client.post("/closes", json={"request_id": "C2"})
    r2 = client.post("/closes", json={"request_id": "C2"})
    assert r2.status_code == 200
    assert r1.json()["close_id"] == r2.json()["close_id"]
    assert r2.json()["expired_reservations_count"] == 1
    # 幂等重放不会重复失效（状态已是 expired，UPDATE 只命中 pending，计数不变）
    assert client.get("/payment-reservations/R1").json()["status"] == "expired"


def test_executed_reservation_survives_new_close_and_reversal(client):
    """已执行付款及后续退票仍遵循现有记录和结算口径。"""
    close_id = setup_author(client)
    reserve(client, "R1", 60000, close_id=close_id)
    client.post("/payment-reservations/R1/execute", json={"payment_id": "PAY-1"})
    client.post("/closes", json={"request_id": "C2"})
    b = client.get("/payment-reservations/R1").json()
    assert b["status"] == "executed" and b["payment_id"] == "PAY-1"
    # 已执行付款仍可退票
    rev = client.post(
        "/payment-reversals", json={"reversal_id": "REV-1", "payment_id": "PAY-1"}
    )
    assert rev.status_code == 201
    assert rev.json()["amount_cents"] == 60000


def test_reservation_replay_after_new_close_returns_original(client):
    close_id = setup_author(client)
    reserve(client, "R1", 1000, close_id=close_id)
    client.post("/closes", json={"request_id": "C2"})
    # 同号同内容重试即使批次已过期/预约已失效，也返回原状态
    r = reserve(client, "R1", 1000, close_id=close_id)
    assert r.status_code == 200
    assert r.json()["status"] == "expired"


# ---------------------------------------------------------------- 结算总览与查询


def test_settlement_includes_reservations(client):
    close_id = setup_author(client)
    reserve(client, "R1", 40000, close_id=close_id)
    reserve(client, "R2", 10000, close_id=close_id)
    client.post("/payment-reservations/R1/execute", json={"payment_id": "PAY-1"})
    client.post("/payment-reservations/R2/cancel")
    s = client.get("/authors/A1/settlement").json()
    by_id = {r["reservation_id"]: r for r in s["reservations"]}
    assert by_id["R1"]["status"] == "executed" and by_id["R1"]["payment_id"] == "PAY-1"
    assert by_id["R2"]["status"] == "cancelled"
    assert s["reserved_cents"] == 0
    assert s["paid_cents"] == 40000
    assert s["available_cents"] == 60000 and s["payable_cents"] == 60000


def test_get_reservation_404(client):
    assert client.get("/payment-reservations/NOPE").status_code == 404


# ---------------------------------------------------------------- 并发


def test_concurrent_reservations_never_over_reserve(client):
    close_id = setup_author(client)

    def make(i):
        return reserve(client, f"R{i}", 20000, close_id=close_id).status_code


    with ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(make, range(10)))
    # 总额度 100000，每笔 20000 → 恰 5 笔成功，其余 400
    assert sorted(codes).count(201) == 5
    assert sorted(codes).count(400) == 5


def test_concurrent_execute_each_reservation_once(client):
    close_id = setup_author(client)
    reserve(client, "R1", 100000, close_id=close_id)

    def execute(i):
        return client.post(
            "/payment-reservations/R1/execute", json={"payment_id": f"PAY-{i}"}
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(execute, range(8)))
    # 仅一笔 201；同号重放不可能（付款编号各异）→ 其余 409，且只登记一笔付款
    assert codes.count(201) == 1
    assert all(c in (201, 409) for c in codes)
    s = client.get("/authors/A1/settlement").json()
    assert len(s["payments"]) == 1
    assert s["paid_cents"] == 100000
