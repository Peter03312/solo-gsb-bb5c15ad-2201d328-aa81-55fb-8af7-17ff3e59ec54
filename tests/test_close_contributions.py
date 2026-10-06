"""作品关账贡献查询（GET /works/{work_id}/close-contributions）端到端测试。

覆盖：
- 分批四类流水（销售/退款/净价更正/价差反向）的数量、册数、金额与批次净版税；
- 冻结流水摘要按入账顺序排列；范围合计；起止均含、缺省查全部已冻结批次；
- 仅已存在作品可查（404）；起止非法/起点大于终点 422；
- 未关账流水不计入；历史批次为关账快照，不受后续入账影响；多作品同批隔离。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, main


@pytest.fixture()
def client():
    db.open_database(":memory:")
    db.init_db()
    with TestClient(main.app) as c:
        yield c


def make_work(client, work_id="W1", tiers=None, authors=None):
    return client.post(
        "/works",
        json={
            "work_id": work_id,
            "title": "测试作品",
            "tiers": tiers
            or [{"threshold": 0, "rate": 0.08}, {"threshold": 1000, "rate": 0.10}],
            "authors": authors
            or [{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 40}],
        },
    )


def make_sale(client, sale_id, work_id="W1", copies=1, price="33.33", ts="2026-09-01T10:00:00"):
    return client.post(
        "/sales",
        json={
            "sale_id": sale_id, "work_id": work_id, "business_time": ts,
            "copies": copies, "net_price": price,
        },
    )


def make_refund(client, refund_id, sale_id, copies):
    return client.post(
        "/refunds", json={"refund_id": refund_id, "sale_id": sale_id, "copies": copies}
    )


def close(client, request_id=None):
    return client.post("/closes", json={"request_id": request_id} if request_id else None)


def contributions(client, work_id="W1", **params):
    return client.get(f"/works/{work_id}/close-contributions", params=params)


def build_three_batches(client):
    """三批场景：

    批1：销售 S1 1200 册（33.33）+ 退款 RF1 250 册 → 净版税 333600-79950=253650
    批2：销售 S2 5 册（20，累计在 10% 档，每册 200 分）→ 净版税 1000
    批3：S1 净价更正 33.33→40（剩 950 册未退，差额 50350）
         + 退款 RF2 100 册（原冲回 26700）+ 价差反向（-5300 贡献）
         → 净版税 50350-26700-5300=18350
    """
    make_work(client)
    assert make_sale(client, "S1", copies=1200).status_code == 201
    assert make_refund(client, "RF1", "S1", 250).status_code == 201
    assert close(client, "C1").status_code == 201
    assert make_sale(
        client, "S2", copies=5, price="20", ts="2026-09-02T10:00:00"
    ).status_code == 201
    assert close(client, "C2").status_code == 201
    r = client.post(
        "/price-corrections",
        json={"correction_id": "COR1", "sale_id": "S1", "new_net_price": 40},
    )
    assert r.status_code == 201 and r.json()["total_delta_cents"] == 50350
    r = make_refund(client, "RF2", "S1", 100)
    assert r.status_code == 201
    rev = r.json()["price_delta_reversal"]
    assert rev["total_reversal_cents"] == 5300
    assert close(client, "C3").status_code == 201


# ---------------------------------------------------------------- 基本口径


def test_per_batch_counts_copies_amounts_and_net_royalty(client):
    build_three_batches(client)
    body = contributions(client).json()
    assert body["work_id"] == "W1"
    assert body["from_close"] == 1 and body["to_close"] == 3
    batches = {b["close_id"]: b for b in body["batches"]}

    b1 = batches[1]
    assert (b1["sales_count"], b1["refunds_count"],
            b1["price_corrections_count"], b1["price_delta_reversals_count"]) == (1, 1, 0, 0)
    assert (b1["sales_copies"], b1["refunds_copies"],
            b1["price_corrections_copies"], b1["price_delta_reversals_copies"]) == (1200, 250, 0, 0)
    assert b1["sales_cents"] == 333600 and b1["refunds_cents"] == 79950
    assert b1["price_corrections_cents"] == 0 and b1["price_delta_reversals_cents"] == 0
    assert b1["net_royalty_cents"] == 333600 - 79950

    b2 = batches[2]
    assert b2["sales_count"] == 1 and b2["sales_copies"] == 5 and b2["sales_cents"] == 1000
    assert b2["net_royalty_cents"] == 1000

    b3 = batches[3]
    assert (b3["sales_count"], b3["refunds_count"],
            b3["price_corrections_count"], b3["price_delta_reversals_count"]) == (0, 1, 1, 1)
    # 更正册数 = 更正时各档未退册数之和（1200-250=950）；反向册数 = 本次被退 100
    assert (b3["sales_copies"], b3["refunds_copies"],
            b3["price_corrections_copies"], b3["price_delta_reversals_copies"]) == (0, 100, 950, 100)
    assert (b3["sales_cents"], b3["refunds_cents"],
            b3["price_corrections_cents"], b3["price_delta_reversals_cents"]) == (
        0, 26700, 50350, 5300)
    assert b3["net_royalty_cents"] == 50350 - 26700 - 5300


def test_lines_ordered_by_entry_seq_with_contribution_signs(client):
    build_three_batches(client)
    b3 = contributions(client).json()["batches"][2]
    assert [l["entry_seq"] for l in b3["lines"]] == sorted(l["entry_seq"] for l in b3["lines"])
    assert [l["seq"] for l in b3["lines"]] == [1, 2, 3]
    assert [(l["type"], l["ref_id"], l["sale_id"], l["copies"],
             l["amount_cents"], l["net_contribution_cents"]) for l in b3["lines"]] == [
        ("price_correction", "COR1", "S1", 950, 50350, 50350),
        ("refund", "RF2", "S1", 100, 26700, -26700),
        ("price_delta_reversal", "RF2", "S1", 100, 5300, -5300),
    ]
    # 摘要类型标签与分成版本
    assert [l["type_label"] for l in b3["lines"]] == ["净价更正", "退款", "价差反向"]
    assert all(l["split_version"] == 1 for l in b3["lines"])
    # 批次净版税 = 各流水带符号贡献之和
    assert sum(l["net_contribution_cents"] for l in b3["lines"]) == b3["net_royalty_cents"]
    b1 = contributions(client).json()["batches"][0]
    assert [(l["type"], l["net_contribution_cents"]) for l in b1["lines"]] == [
        ("sale", 333600), ("refund", -79950)
    ]


def test_range_totals(client):
    build_three_batches(client)
    totals = contributions(client).json()["totals"]
    assert totals == {
        "batch_count": 3,
        "sales_count": 2, "refunds_count": 2,
        "price_corrections_count": 1, "price_delta_reversals_count": 1,
        "sales_copies": 1205, "refunds_copies": 350,
        "price_corrections_copies": 950, "price_delta_reversals_copies": 100,
        "sales_cents": 334600, "refunds_cents": 106650,
        "price_corrections_cents": 50350, "price_delta_reversals_cents": 5300,
        "net_royalty_cents": 253650 + 1000 + 18350,
    }
    # 合计净版税 = 各批净版税之和
    assert totals["net_royalty_cents"] == 273000
    assert (totals["sales_cents"] - totals["refunds_cents"]
            + totals["price_corrections_cents"]
            - totals["price_delta_reversals_cents"]) == totals["net_royalty_cents"]


# ---------------------------------------------------------------- 范围参数


def test_range_inclusive_both_ends(client):
    build_three_batches(client)
    body = contributions(client, from_close=2, to_close=3).json()
    assert body["from_close"] == 2 and body["to_close"] == 3
    assert [b["close_id"] for b in body["batches"]] == [2, 3]
    assert body["totals"]["batch_count"] == 2
    assert body["totals"]["net_royalty_cents"] == 1000 + 18350

    body = contributions(client, from_close=2, to_close=2).json()
    assert [b["close_id"] for b in body["batches"]] == [2]
    assert body["totals"]["sales_cents"] == 1000

    # 仅给一端
    assert [b["close_id"] for b in contributions(client, from_close=3).json()["batches"]] == [3]
    body = contributions(client, to_close=1).json()
    assert body["from_close"] == 1 and [b["close_id"] for b in body["batches"]] == [1]


def test_range_extending_beyond_latest_returns_existing(client):
    build_three_batches(client)
    body = contributions(client, from_close=4, to_close=99).json()
    assert body["batches"] == []
    assert body["totals"]["batch_count"] == 0
    assert body["totals"]["net_royalty_cents"] == 0


def test_no_closes_yet(client):
    make_work(client)
    body = contributions(client).json()
    assert body["batches"] == []
    assert body["from_close"] is None and body["to_close"] is None
    assert body["totals"]["batch_count"] == 0


# ---------------------------------------------------------------- 错误码


def test_work_not_found_404(client):
    assert contributions(client, "NOPE").status_code == 404
    assert contributions(client, "NOPE", from_close=1, to_close=2).status_code == 404


def test_invalid_range_422(client):
    build_three_batches(client)
    # 非正整数
    assert contributions(client, from_close=0).status_code == 422
    assert contributions(client, to_close=0).status_code == 422
    assert contributions(client, from_close=-1, to_close=2).status_code == 422
    # 非整数
    assert contributions(client, from_close="x").status_code == 422
    # 起点大于终点
    r = contributions(client, from_close=3, to_close=2)
    assert r.status_code == 422
    assert "3" in r.json()["detail"]


# ---------------------------------------------------------------- 冻结语义


def test_unclosed_entries_excluded_and_history_frozen(client):
    build_three_batches(client)
    # 关账后补录的流水（更正、退款）不计入历史批次，也不出现在全量查询
    make_sale(client, "S3", copies=7, ts="2026-10-01T10:00:00")
    make_refund(client, "RF3", "S2", 2)
    client.post(
        "/price-corrections",
        json={"correction_id": "COR2", "sale_id": "S1", "new_net_price": 35},
    )
    body = contributions(client).json()
    assert body["totals"]["batch_count"] == 3
    totals = body["totals"]
    assert totals["sales_count"] == 2 and totals["sales_copies"] == 1205
    assert totals["refunds_count"] == 2 and totals["price_corrections_count"] == 1
    # 历史批次快照不变
    b1 = body["batches"][0]
    assert b1["sales_cents"] == 333600 and b1["refunds_cents"] == 79950
    b3 = body["batches"][2]
    assert b3["price_corrections_cents"] == 50350

    # 补录流水进入下一批后：历史三批仍不变，新批含全部补录
    assert close(client, "C4").status_code == 201
    body = contributions(client, from_close=1, to_close=3).json()
    assert [b["close_id"] for b in body["batches"]] == [1, 2, 3]
    assert body["totals"]["sales_copies"] == 1205
    b4 = contributions(client, from_close=4, to_close=4).json()["batches"][0]
    # S2 未被更正，RF3 不产生价差反向流水
    assert (b4["sales_count"], b4["refunds_count"],
            b4["price_corrections_count"], b4["price_delta_reversals_count"]) == (1, 1, 1, 0)
    assert b4["sales_copies"] == 7 and b4["refunds_copies"] == 2
    # COR2 是对 S1 第二次更正（40→35），作用于剩余 850 册未退册数
    assert b4["price_corrections_copies"] == 850
    assert b4["price_corrections_cents"] < 0


def test_multi_work_isolation_in_same_batch(client):
    build_three_batches(client)
    # 另一作品与 W1 的流水同批关账，查询互不混入
    make_work(
        client, "W2",
        tiers=[{"threshold": 0, "rate": 0.05}],
        authors=[{"author_id": "B1", "ratio": 100}],
    )
    make_sale(client, "S9", work_id="W2", copies=10, price="100", ts="2026-10-02T10:00:00")
    close(client, "C4")

    w1 = contributions(client, "W1", from_close=4, to_close=4).json()
    assert w1["batches"] == []  # 第 4 批中无 W1 流水
    w2 = contributions(client, "W2").json()
    assert len(w2["batches"]) == 1 and w2["batches"][0]["close_id"] == 4
    assert w2["batches"][0]["sales_copies"] == 10
    assert w2["batches"][0]["sales_cents"] == 10 * (100 * 0.05) * 100  # 每册 500 分
    assert w2["totals"]["batch_count"] == 1


def test_downward_price_correction_is_negative(client):
    # 向下更正（冲回）：更正金额为负，贡献同样为负
    make_work(client)
    make_sale(client, "S1", copies=10, price="100")  # 8% 档，每册 800 分
    close(client, "C1")
    r = client.post(
        "/price-corrections",
        json={"correction_id": "COR-DOWN", "sale_id": "S1", "new_net_price": 50},
    )
    assert r.status_code == 201
    assert r.json()["total_delta_cents"] == 10 * (400 - 800)  # -4000
    close(client, "C2")
    b2 = contributions(client, from_close=2, to_close=2).json()["batches"][0]
    assert b2["price_corrections_count"] == 1
    assert b2["price_corrections_copies"] == 10
    assert b2["price_corrections_cents"] == -4000
    assert b2["net_royalty_cents"] == -4000
    line = b2["lines"][0]
    assert line["amount_cents"] == -4000 and line["net_contribution_cents"] == -4000


def test_range_totals_consistent_with_frozen_close_report(client):
    build_three_batches(client)
    # 作品批次净版税 = 该批关账快照中该作品作者净额之和（与报表口径一致）
    body = contributions(client).json()
    for b in body["batches"]:
        report = client.get(f"/closes/{b['close_id']}").json()
        author_net = sum(
            t["amount_cents"] for t in report["author_totals"]
        )  # 本场景只有一个作品
        assert b["net_royalty_cents"] == author_net
        assert b["net_royalty_cents"] == report["total_author_net_cents"]
