"""付款预约：额度预占、直接付款扣除占额、执行/取消/关账失效、幂等与并发安全。"""
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


def make_work(client):
    return client.post(
        "/works",
        json={
            "work_id": "W1",
            "title": "测试作品",
            "tiers": [{"threshold": 0, "rate": 0.08}],
            "authors": [{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 40}],
        },
    )


def make_sale(client, sale_id, copies, price="10.00"):
    # 每册 10×8%=80 分；A1 60% → 每册 48 分
    return client.post(
        "/sales",
        json={
            "sale_id": sale_id,
            "work_id": "W1",
            "business_time": "2026-09-01T10:00:00",
            "copies": copies,
            "net_price": price,
        },
    )


def reserve(client, reservation_id, author_id, close_id, amount):
    return client.post(
        "/payment-reservations",
        json={
            "reservation_id": reservation_id,
            "author_id": author_id,
            "close_id": close_id,
            "amount_cents": amount,
        },
    )


def execute(client, reservation_id, payment_id):
    return client.post(
        f"/payment-reservations/{reservation_id}/execute",
        json={"payment_id": payment_id},
    )


def cancel(client, reservation_id, reason=None):
    body = {} if reason is None else {"reason": reason}
    return client.post(f"/payment-reservations/{reservation_id}/cancel", json=body)


def pay(client, payment_id, author_id, close_id, amount):
    return client.post(
        "/payments",
        json={
            "payment_id": payment_id,
            "author_id": author_id,
            "close_id": close_id,
            "amount_cents": amount,
        },
    )


@pytest.fixture()
def closed(client):
    """100 册销售并关账：A1 应计 4800 分、A2 应计 3200 分。"""
    make_work(client)
    make_sale(client, "S1", 100)
    close_id = client.post("/closes", json={"request_id": "C1"}).json()["close_id"]
    return close_id


# ---------------------------------------------------------------- 创建与占额


def test_reserve_happy_path_and_snapshot(closed, client):
    r = reserve(client, "RV1", "A1", closed, 3000)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending" and body["payment_id"] is None and body["payment"] is None
    assert body["accrued_cents"] == 4800 and body["paid_cents"] == 0
    assert body["reserved_cents"] == 0  # 不含本笔
    assert body["payable_cents"] == 1800  # 扣除本笔后的可付额
    assert body["available_cents"] == 1800  # 作者当前可用额
    assert body["latest_close_id"] == closed
    assert body["executed_at"] is None and body["canceled_at"] is None and body["expired_at"] is None

    # 结算总览：待执行占额 3000，可付额口径不变，当前可用额扣除占额
    s = client.get("/authors/A1/settlement").json()
    assert s["accrued_cents"] == 4800 and s["paid_cents"] == 0
    assert s["reserved_cents"] == 3000
    assert s["payable_cents"] == 4800  # 既有口径：max(应计 − 有效已付, 0)
    assert s["available_cents"] == 1800
    assert [x["reservation_id"] for x in s["reservations"]] == ["RV1"]


def test_reserved_hold_blocks_direct_payment(closed, client):
    assert reserve(client, "RV1", "A1", closed, 3000).status_code == 201
    # 直接付款不得绕过预约：可用额仅 1800
    assert pay(client, "P1", "A1", closed, 1800).status_code == 201
    assert pay(client, "P2", "A1", closed, 1).status_code == 400
    # 付款快照的 payable_cents 同样扣除占额
    p1 = client.get("/payments/P1").json()
    assert p1["paid_before_cents"] == 0 and p1["payable_cents"] == 1800
    # 预约仍可执行：排除自身占额后余额 3000 恰够
    r = execute(client, "RV1", "PAY-RV1")
    assert r.status_code == 201 and r.json()["status"] == "executed"
    s = client.get("/authors/A1/settlement").json()
    assert s["paid_cents"] == 4800 and s["reserved_cents"] == 0 and s["available_cents"] == 0


def test_multiple_reservations_stack_and_overbooking_rejected(closed, client):
    assert reserve(client, "RV1", "A1", closed, 3000).status_code == 201
    r = reserve(client, "RV2", "A1", closed, 1800)  # 恰好用完
    assert r.status_code == 201 and r.json()["available_cents"] == 0
    assert reserve(client, "RV3", "A1", closed, 1).status_code == 400  # 超额拒绝
    # 不同作者额度互不影响
    r = reserve(client, "RV-A2", "A2", closed, 3200)
    assert r.status_code == 201 and r.json()["available_cents"] == 0


def test_reserve_validation_errors(closed, client):
    assert reserve(client, "RVX", "NOBODY", closed, 100).status_code == 404
    assert reserve(client, "RVX", "A1", 999, 100).status_code == 404
    # 新关账后旧批次过期 → 409（应计为累计口径，新批次上仍可预约旧批次累计额）
    c2 = client.post("/closes", json={"request_id": "C2"}).json()["close_id"]
    assert reserve(client, "RV-OLD", "A1", closed, 100).status_code == 409
    # 最新批次累计应计仍为 4800：预约 4800 成功，再约 1 分则超额 → 400
    assert reserve(client, "RV-FULL", "A1", c2, 4800).status_code == 201
    assert reserve(client, "RVX", "A1", c2, 100).status_code == 400
    # 非正整数金额 → 422
    r = client.post(
        "/payment-reservations",
        json={"reservation_id": "RVY", "author_id": "A1", "close_id": c2, "amount_cents": 0},
    )
    assert r.status_code == 422


def test_reserve_idempotent_replay_and_conflict(closed, client):
    body = reserve(client, "RV1", "A1", closed, 3000).json()
    r = reserve(client, "RV1", "A1", closed, 3000)
    assert r.status_code == 200 and r.json() == body
    # 同号异内容（金额/作者/批次不同）→ 409
    assert reserve(client, "RV1", "A1", closed, 3001).status_code == 409
    assert reserve(client, "RV1", "A2", closed, 3000).status_code == 409
    assert reserve(client, "RV1", "A1", closed + 1, 3000).status_code in (404, 409)
    # 被拒绝的异内容不影响原预约
    assert client.get("/payment-reservations/RV1").json()["amount_cents"] == 3000


def test_reserve_replay_after_expire_returns_original(closed, client):
    body = reserve(client, "RV1", "A1", closed, 3000).json()
    client.post("/closes", json={"request_id": "C2"})  # 预约失效
    # 同号同内容重试仍返回原状态（此时为 expired）
    r = reserve(client, "RV1", "A1", closed, 3000)
    assert r.status_code == 200
    got = r.json()
    assert got["status"] == "expired"
    assert got["reservation_id"] == body["reservation_id"]
    assert got["accrued_cents"] == 4800  # 创建时快照不变


# ---------------------------------------------------------------- 执行


def test_execute_registers_payment_and_releases_hold(closed, client):
    reserve(client, "RV1", "A1", closed, 3000)
    reserve(client, "RV2", "A1", closed, 1000)  # 占额后可用 800
    r = execute(client, "RV1", "PAY1")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "executed" and body["payment_id"] == "PAY1"
    assert body["payment"] == client.get("/payments/PAY1").json()
    assert body["payment"]["amount_cents"] == 3000
    assert body["payment"]["close_id"] == closed
    assert body["executed_at"] is not None
    # 占额解除：仅剩 RV2 的 1000；可用 = 4800 − 3000(已付) − 1000 = 800
    assert body["current_paid_cents"] == 3000
    assert body["current_reserved_cents"] == 1000
    assert body["available_cents"] == 800
    s = client.get("/authors/A1/settlement").json()
    assert s["paid_cents"] == 3000 and s["reserved_cents"] == 1000
    assert s["available_cents"] == 800


def test_execute_replay_and_payment_id_conflicts(closed, client):
    reserve(client, "RV1", "A1", closed, 1000)
    reserve(client, "RV2", "A1", closed, 1000)
    first = execute(client, "RV1", "PAY1")
    assert first.status_code == 201
    # 重复执行（同付款编号）→ 200 返回原付款
    r = execute(client, "RV1", "PAY1")
    assert r.status_code == 200 and r.json() == first.json()
    # 换付款编号执行同一预约 → 409
    assert execute(client, "RV1", "PAY-OTHER").status_code == 409
    # 用已存在的付款编号执行另一预约 → 409（直接付款编号也算）
    assert pay(client, "PAY-DIRECT", "A1", closed, 500).status_code == 201
    assert execute(client, "RV2", "PAY1").status_code == 409
    assert execute(client, "RV2", "PAY-DIRECT").status_code == 409
    # 未被消费的预约仍可执行（新付款编号）
    assert execute(client, "RV2", "PAY2").status_code == 201


def test_execute_terminal_state_transitions_rejected(closed, client):
    reserve(client, "RV1", "A1", closed, 1000)
    assert cancel(client, "RV1", reason="计划变更").status_code == 200
    # 已取消不得执行
    assert execute(client, "RV1", "PAY1").status_code == 409
    # 已取消重复取消 → 200 原状态（幂等）
    again = cancel(client, "RV1")
    assert again.status_code == 200 and again.json()["status"] == "canceled"
    assert again.json()["cancel_reason"] == "计划变更"

    # 已执行不得取消
    reserve(client, "RV2", "A1", closed, 1000)
    execute(client, "RV2", "PAY2")
    assert cancel(client, "RV2").status_code == 409


def test_execute_unknown_reservation_404(closed, client):
    assert execute(client, "NOPE", "PAY1").status_code == 404
    assert cancel(client, "NOPE").status_code == 404
    assert client.get("/payment-reservations/NOPE").status_code == 404


# ---------------------------------------------------------------- 取消


def test_cancel_releases_hold(closed, client):
    reserve(client, "RV1", "A1", closed, 3000)
    assert reserve(client, "RV2", "A1", closed, 1000).status_code == 201
    r = cancel(client, "RV1")
    assert r.status_code == 200 and r.json()["status"] == "canceled"
    assert r.json()["canceled_at"] is not None
    # 占额释放：可用恢复为 3800
    s = client.get("/authors/A1/settlement").json()
    assert s["reserved_cents"] == 1000 and s["available_cents"] == 3800
    # 释放后直接付款可使用该额度
    assert pay(client, "P1", "A1", closed, 3800).status_code == 201
    # 已取消预约不出现在占额中，直接付款成功后无超付
    assert pay(client, "P2", "A1", closed, 1).status_code == 400


# ---------------------------------------------------------------- 关账失效


def test_new_close_expires_pending_reservations(closed, client):
    reserve(client, "RV1", "A1", closed, 3000)
    reserve(client, "RV2", "A2", closed, 1000)
    r = client.post("/closes", json={"request_id": "C2"})
    assert r.status_code == 201
    rep = r.json()
    assert rep["expired_payment_reservations_count"] == 2
    got = client.get("/payment-reservations/RV1").json()
    assert got["status"] == "expired" and got["expired_close_id"] == rep["close_id"]
    assert got["expired_at"] is not None and got["payment_id"] is None
    # 占额释放：新批次净额为 0，但旧批次应计仍在；可用额按最新批次累计应计重算
    s = client.get("/authors/A1/settlement").json()
    assert s["reserved_cents"] == 0 and s["available_cents"] == 4800
    # 终态不可转换
    assert execute(client, "RV1", "PAY1").status_code == 409
    assert cancel(client, "RV1").status_code == 409
    # 失效后可在新批次重新预约同一金额（新预约号）
    assert reserve(client, "RV1-NEW", "A1", rep["close_id"], 4800).status_code == 201


def test_close_does_not_expire_executed_and_reversal_still_works(closed, client):
    reserve(client, "RV1", "A1", closed, 3000)
    execute(client, "RV1", "PAY1")
    reserve(client, "RV2", "A1", closed, 800)
    cancel(client, "RV2")
    rep = client.post("/closes", json={"request_id": "C2"}).json()
    assert rep["expired_payment_reservations_count"] == 0
    got = client.get("/payment-reservations/RV1").json()
    assert got["status"] == "executed" and got["payment_id"] == "PAY1"
    assert client.get("/payment-reservations/RV2").json()["status"] == "canceled"
    # 已执行付款仍可按现有口径退票，退票后金额恢复为可用
    r = client.post(
        "/payment-reversals",
        json={"reversal_id": "REV1", "payment_id": "PAY1"},
    )
    assert r.status_code == 201 and r.json()["amount_cents"] == 3000
    s = client.get("/authors/A1/settlement").json()
    assert s["paid_cents"] == 0 and s["available_cents"] == 4800
    assert s["reserved_cents"] == 0


def test_expired_reservations_count_only_for_that_close(closed, client):
    reserve(client, "RV1", "A1", closed, 100)
    c2 = client.post("/closes", json={"request_id": "C2"}).json()["close_id"]
    assert client.get(f"/closes/{closed}").json()["expired_payment_reservations_count"] == 0
    assert client.get(f"/closes/{c2}").json()["expired_payment_reservations_count"] == 1
    reserve(client, "RV2", "A1", c2, 200)
    c3 = client.post("/closes", json={"request_id": "C3"}).json()["close_id"]
    assert client.get(f"/closes/{c3}").json()["expired_payment_reservations_count"] == 1
    assert client.get(f"/closes/{c2}").json()["expired_payment_reservations_count"] == 1


# ---------------------------------------------------------------- 并发


def test_concurrent_reservations_never_overbook(closed, client):
    def do_reserve(i):
        return reserve(client, f"RC{i}", "A1", closed, 500)  # 可约 4800 → 至多 9 笔

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(do_reserve, range(20)))
    created = [r for r in results if r.status_code == 201]
    rejected = [r for r in results if r.status_code == 400]
    assert len(created) == 9 and len(rejected) == 11  # 9×500=4500，余 300
    s = client.get("/authors/A1/settlement").json()
    assert s["reserved_cents"] == 4500 and s["available_cents"] == 300


def test_concurrent_execute_same_reservation_single_success(closed, client):
    reserve(client, "RV1", "A1", closed, 1000)

    def do_execute(i):
        return execute(client, "RV1", f"PAY{i}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(do_execute, range(20)))
    # 只有一个线程能成功；同付款编号串行下也至多一个 201，其余换编号冲突 409
    assert sum(1 for r in results if r.status_code == 201) == 1
    assert sum(1 for r in results if r.status_code == 409) == 19
    got = client.get("/payment-reservations/RV1").json()
    assert got["status"] == "executed"
    payments = client.get("/authors/A1/settlement").json()["payments"]
    assert [p["payment_id"] for p in payments] == [got["payment_id"]]


def test_concurrent_execute_distinct_reservations_unique_payment_ids(closed, client):
    for i in range(8):
        reserve(client, f"RV{i}", "A1", closed, 500)  # 8×500=4000 ≤ 4800

    def do_execute(i):
        return execute(client, f"RV{i}", "PAY-SHARED")  # 全部抢同一付款编号

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(do_execute, range(8)))
    assert sum(1 for r in results if r.status_code == 201) == 1
    assert sum(1 for r in results if r.status_code == 409) == 7
    executed = client.get("/authors/A1/settlement").json()["reservations"]
    assert sum(1 for x in executed if x["status"] == "executed") == 1
    assert sum(1 for x in executed if x["status"] == "pending") == 7


def test_reserved_funds_guaranteed_across_executions_and_direct_pays(closed, client):
    # 占满全部额度：RV1 4000 + RV2 800 = 4800；直接付款无可用额
    assert reserve(client, "RV1", "A1", closed, 4000).status_code == 201
    assert reserve(client, "RV2", "A1", closed, 800).status_code == 201
    assert pay(client, "P1", "A1", closed, 1).status_code == 400
    # 先执行 RV2（其占额转为已付），RV1 的占额不受影响仍可执行：
    # 不变量 有效已付 + 待执行占额 ≤ 应计，故 pending 预约在批次有效期内必可执行
    assert execute(client, "RV2", "PAY2").status_code == 201
    r = execute(client, "RV1", "PAY1")
    assert r.status_code == 201
    s = client.get("/authors/A1/settlement").json()
    assert s["paid_cents"] == 4800 and s["reserved_cents"] == 0 and s["available_cents"] == 0
    # 执行登记的付款仍可按现有口径退票
    client.post("/payment-reversals", json={"reversal_id": "REV1", "payment_id": "PAY1"})
    s = client.get("/authors/A1/settlement").json()
    assert s["paid_cents"] == 800 and s["available_cents"] == 4000


def test_cancel_without_body_is_accepted(closed, client):
    reserve(client, "RV1", "A1", closed, 100)
    # 完全不带请求体也应取消成功
    r = client.post("/payment-reservations/RV1/cancel")
    assert r.status_code == 200 and r.json()["status"] == "canceled"
    assert r.json()["cancel_reason"] is None
    # 空 JSON 对象同样合法
    reserve(client, "RV2", "A1", closed, 100)
    r = client.post("/payment-reservations/RV2/cancel", json={})
    assert r.status_code == 200


def test_execute_requires_payment_id(closed, client):
    reserve(client, "RV1", "A1", closed, 100)
    r = client.post("/payment-reservations/RV1/execute", json={})
    assert r.status_code == 422
    assert client.get("/payment-reservations/RV1").json()["status"] == "pending"


# ---------------------------------------------------------------- 退票不影响占额口径


def test_reversal_restores_amount_behind_hold(closed, client):
    # 直接付款 2000 后退票；预约占额与可用额按有效已付口径联动
    assert pay(client, "P1", "A1", closed, 2000).status_code == 201
    assert reserve(client, "RV1", "A1", closed, 2800).status_code == 201  # 4800-2000
    client.post("/payment-reversals", json={"reversal_id": "REV1", "payment_id": "P1"})
    got = client.get("/payment-reservations/RV1").json()
    # 退票使有效已付归零：可用 = 4800 - 0 - 2800 = 2000
    assert got["current_paid_cents"] == 0 and got["available_cents"] == 2000
    # 占额仍在：直接付款最多 2000
    assert pay(client, "P2", "A1", closed, 2001).status_code == 400
    assert execute(client, "RV1", "PAY-RV1").status_code == 201
