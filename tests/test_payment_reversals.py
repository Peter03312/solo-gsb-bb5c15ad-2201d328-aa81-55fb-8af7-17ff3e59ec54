"""付款冲回（银行退票）：整笔冲回、登记时结算快照、幂等重试与并发安全。"""
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


def reverse(client, reversal_id, payment_id):
    return client.post(
        "/payment-reversals",
        json={"reversal_id": reversal_id, "payment_id": payment_id},
    )


# ---------------------------------------------------------------- 整笔冲回与登记时快照


def test_reversal_happy_path_and_snapshot(client):
    make_work(client)
    make_sale(client, "S1", 100)  # 100×10×8% = 8000 分；A1 4800 / A2 3200
    c1 = client.post("/closes", json={"request_id": "C1"}).json()["close_id"]
    assert pay(client, "P1", "A1", c1, 4800).status_code == 201

    r = reverse(client, "REV-1", "P1")
    assert r.status_code == 201
    b = r.json()
    assert b["reversal_id"] == "REV-1" and b["payment_id"] == "P1"
    assert b["author_id"] == "A1"
    assert b["amount_cents"] == 4800  # 整笔冲回：冲回金额 = 原付款金额
    # 关联付款：返回原付款提交时快照
    assert b["payment"]["payment_id"] == "P1"
    assert b["payment"]["amount_cents"] == 4800
    assert b["payment"]["accrued_cents"] == 4800
    assert b["payment"]["paid_after_cents"] == 4800
    # 登记时的作者结算快照（按最新关账批次重算）
    assert b["latest_close_id"] == c1
    assert b["accrued_cents"] == 4800
    assert b["paid_before_cents"] == 4800
    assert b["paid_after_cents"] == 0
    assert b["payable_cents"] == 4800
    assert b["negative_balance_cents"] == 0

    # 结算总览：有效已付 = 付款总额 − 冲回总额 = 0，可付恢复，冲回记录可查
    s = client.get("/authors/A1/settlement").json()
    assert s["paid_cents"] == 0 and s["reversed_cents"] == 4800
    assert s["payable_cents"] == 4800 and s["negative_balance_cents"] == 0
    assert [x["reversal_id"] for x in s["reversals"]] == ["REV-1"]
    assert s["reversals"][0]["payment_id"] == "P1"
    assert s["reversals"][0]["amount_cents"] == 4800
    assert s["reversals"][0]["paid_after_cents"] == 0

    # 原付款及其提交时快照保留不改写
    p = client.get("/payments/P1").json()
    assert p["amount_cents"] == 4800 and p["paid_after_cents"] == 4800


def test_reversal_idempotent_replay_and_conflicts(client):
    make_work(client)
    make_sale(client, "S1", 100)
    c1 = client.post("/closes").json()["close_id"]
    pay(client, "P1", "A1", c1, 1000)
    pay(client, "P2", "A1", c1, 2000)

    r1 = reverse(client, "REV-1", "P1")
    assert r1.status_code == 201
    # 同冲回编号同内容重试 → 200 返回原结果
    r2 = reverse(client, "REV-1", "P1")
    assert r2.status_code == 200 and r2.json() == r1.json()
    # 同冲回编号异内容 → 409
    assert reverse(client, "REV-1", "P2").status_code == 409
    # 换编号重复冲回同一付款 → 409（每笔付款至多冲回一次）
    assert reverse(client, "REV-2", "P1").status_code == 409
    # 被拒绝的提交不占用冲回编号：REV-2 仍可用于其他付款
    assert reverse(client, "REV-2", "P2").status_code == 201
    s = client.get("/authors/A1/settlement").json()
    assert s["reversed_cents"] == 3000 and s["paid_cents"] == 0


def test_reversal_unknown_payment_404_and_validation_422(client):
    make_work(client)
    make_sale(client, "S1", 100)
    c1 = client.post("/closes").json()["close_id"]
    pay(client, "P1", "A1", c1, 1000)
    # 原付款不存在 → 404
    assert reverse(client, "REV-1", "NOPE").status_code == 404
    # 空编号 → 422
    assert reverse(client, "", "P1").status_code == 422
    assert reverse(client, "REV-1", "").status_code == 422
    # 缺字段 → 422
    assert client.post("/payment-reversals", json={"reversal_id": "REV-1"}).status_code == 422
    # 404/422 不占用冲回编号
    assert reverse(client, "REV-1", "P1").status_code == 201
    # 查询不存在的冲回 → 404
    assert client.get("/payment-reversals/NOPE").status_code == 404


def test_reversal_restores_payable_and_repayment(client):
    """冲回后可付额按最新关账快照重算：被退回金额恢复为可付，可重新付款且仍不超付。"""
    make_work(client)
    make_sale(client, "S1", 100)  # A1 应计 4800
    c1 = client.post("/closes").json()["close_id"]
    assert pay(client, "P1", "A1", c1, 4800).status_code == 201
    assert pay(client, "P2", "A1", c1, 1).status_code == 400  # 已付满

    assert reverse(client, "REV-1", "P1").status_code == 201
    # 可付额恢复 4800；超额仍拒
    assert pay(client, "P3", "A1", c1, 4801).status_code == 400
    r = pay(client, "P3", "A1", c1, 4800)
    assert r.status_code == 201
    assert r.json()["paid_before_cents"] == 0  # 有效已付已扣除冲回
    s = client.get("/authors/A1/settlement").json()
    assert s["paid_cents"] == 4800 and s["payable_cents"] == 0


def test_reversal_recomputes_negative_balance(client):
    """关账后退款形成待抵扣负余额；冲回按最新关账快照重算，负余额随之清零。"""
    make_work(client)
    make_sale(client, "S1", 100)  # A1 4800
    c1 = client.post("/closes").json()["close_id"]
    pay(client, "P1", "A1", c1, 4800)
    # 关账后全额退款 → 应计 0 < 已付 4800 → 待抵扣负余额 4800
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 100})
    c2 = client.post("/closes").json()["close_id"]
    s = client.get("/authors/A1/settlement").json()
    assert s["negative_balance_cents"] == 4800 and s["payable_cents"] == 0

    # 银行退票冲回：快照按最新关账批次（c2）重算 → 有效已付 0，负余额清零
    r = reverse(client, "REV-1", "P1")
    assert r.status_code == 201
    b = r.json()
    assert b["latest_close_id"] == c2
    assert b["accrued_cents"] == 0
    assert b["paid_before_cents"] == 4800
    assert b["paid_after_cents"] == 0
    assert b["negative_balance_cents"] == 0 and b["payable_cents"] == 0
    s = client.get("/authors/A1/settlement").json()
    assert s["negative_balance_cents"] == 0 and s["paid_cents"] == 0


def test_reversal_snapshot_frozen_and_history_untouched(client):
    """后续关账不改变旧冲回结果；既有关账报表与原付款快照均保留。"""
    make_work(client)
    make_sale(client, "S1", 100)
    c1 = client.post("/closes", json={"request_id": "C1"}).json()["close_id"]
    pay(client, "P1", "A1", c1, 4800)
    b = reverse(client, "REV-1", "P1").json()

    # 后续销售与关账不改变已登记的冲回快照
    make_sale(client, "S2", 100)
    c2 = client.post("/closes", json={"request_id": "C2"}).json()["close_id"]
    r = reverse(client, "REV-1", "P1")
    assert r.status_code == 200 and r.json() == b  # 重放返回原快照
    assert client.get("/payment-reversals/REV-1").json() == b
    assert b["latest_close_id"] == c1

    # 既有关账报表与原付款提交时快照不改写
    rep1 = client.get(f"/closes/{c1}").json()
    assert rep1["author_totals"] == [
        {"author_id": "A1", "amount_cents": 4800},
        {"author_id": "A2", "amount_cents": 3200},
    ]
    p = client.get("/payments/P1").json()
    assert p["accrued_cents"] == 4800 and p["paid_after_cents"] == 4800

    # 新批次上按最新快照结算：有效已付 0，可付 = 累计应计 9600
    s = client.get("/authors/A1/settlement").json()
    assert s["accrued_cents"] == 9600 and s["paid_cents"] == 0
    assert s["payable_cents"] == 9600
    assert pay(client, "P2", "A1", c2, 9600).status_code == 201


def test_settlement_lists_reversals_per_author(client):
    """冲回记录按作者独立核算，可在各自结算总览中查询。"""
    make_work(client)
    make_sale(client, "S1", 100)  # A1 4800 / A2 3200
    c1 = client.post("/closes").json()["close_id"]
    pay(client, "P1", "A1", c1, 1000)
    pay(client, "P2", "A1", c1, 2000)
    pay(client, "P3", "A2", c1, 3200)
    reverse(client, "REV-1", "P1")
    reverse(client, "REV-2", "P2")
    reverse(client, "REV-3", "P3")

    s = client.get("/authors/A1/settlement").json()
    assert [x["reversal_id"] for x in s["reversals"]] == ["REV-1", "REV-2"]
    assert s["reversed_cents"] == 3000 and s["paid_cents"] == 0
    assert s["payable_cents"] == 4800
    # A2 的冲回独立核算
    s2 = client.get("/authors/A2/settlement").json()
    assert [x["reversal_id"] for x in s2["reversals"]] == ["REV-3"]
    assert s2["paid_cents"] == 0 and s2["payable_cents"] == 3200
    # 按冲回编号查询
    b = client.get("/payment-reversals/REV-3").json()
    assert b["payment_id"] == "P3" and b["author_id"] == "A2"
    assert b["amount_cents"] == 3200


# ---------------------------------------------------------------- 并发


def test_concurrent_reversal_and_payments_never_overpay(client):
    """冲回与新付款并发：按提交顺序原子判定，有效已付额始终在 [0, 应计] 内，不超付。"""
    make_work(client)
    make_sale(client, "S1", 200)  # 200×10×8% = 16000 分；A1 9600
    c1 = client.post("/closes").json()["close_id"]
    pay(client, "P0", "A1", c1, 4600)

    def do_reverse():
        return reverse(client, "REV-1", "P0")

    def do_pay(i):
        return pay(client, f"CP{i}", "A1", c1, 500)

    with ThreadPoolExecutor(max_workers=9) as pool:
        futures = [pool.submit(do_reverse)]
        futures += [pool.submit(do_pay, i) for i in range(20)]
        results = [f.result() for f in futures]

    rev, pays = results[0], results[1:]
    assert rev.status_code == 201
    assert all(r.status_code in (201, 400) for r in pays)
    # 不超付：有效已付 = 付款总额 − 冲回总额，且不超过应计 9600
    s = client.get("/authors/A1/settlement").json()
    gross = 4600 + 500 * sum(1 for r in pays if r.status_code == 201)
    assert s["paid_cents"] == gross - 4600
    assert 0 <= s["paid_cents"] <= 9600
    assert s["payable_cents"] == 9600 - s["paid_cents"]


def test_concurrent_duplicate_reversals_only_one_succeeds(client):
    """每笔付款至多冲回一次：并发换编号冲回同一付款至多一笔成功；同号重放并发安全。"""
    make_work(client)
    make_sale(client, "S1", 100)
    c1 = client.post("/closes").json()["close_id"]
    pay(client, "P1", "A1", c1, 4800)

    # 换编号并发冲回同一付款：恰好一笔 201，其余 409
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: reverse(client, f"REV-{i}", "P1"), range(8)))
    assert sum(1 for r in results if r.status_code == 201) == 1
    assert sum(1 for r in results if r.status_code == 409) == 7
    s = client.get("/authors/A1/settlement").json()
    assert len(s["reversals"]) == 1 and s["reversed_cents"] == 4800

    # 同冲回编号同内容并发重放：一笔 201，其余 200，且返回内容完全一致
    pay(client, "P2", "A1", c1, 4800)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: reverse(client, "REV-X", "P2"), range(8)))
    assert sum(1 for r in results if r.status_code == 201) == 1
    assert sum(1 for r in results if r.status_code == 200) == 7
    assert len({r.text for r in results}) == 1
