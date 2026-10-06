"""渠道对账 API 测试：逐笔核对、整批拒绝、幂等冻结、快照隔离与不改写既有账务。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import db, main


@pytest.fixture()
def client():
    db.open_database(":memory:")
    db.init_db()
    with TestClient(main.app) as c:
        yield c


def _work(client, work_id="W1", tiers=None, authors=None):
    return client.post(
        "/works",
        json={
            "work_id": work_id,
            "title": "对账测试作品",
            "tiers": tiers
            or [{"threshold": 0, "rate": 0.08}, {"threshold": 1000, "rate": 0.10}],
            "authors": authors
            or [{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 40}],
        },
    )


def _sale(client, sale_id, copies=10, price="33.33", ts="2026-09-01T10:00:00", work_id="W1"):
    return client.post(
        "/sales",
        json={
            "sale_id": sale_id,
            "work_id": work_id,
            "business_time": ts,
            "copies": copies,
            "net_price": price,
        },
    )


def _refund(client, refund_id, sale_id, copies):
    return client.post(
        "/refunds",
        json={"refund_id": refund_id, "sale_id": sale_id, "copies": copies},
    )


def _recon(client, batch_no="RC-1", seq_from=0, seq_to=None, receipts=None, channel="CH-X"):
    if seq_to is None:
        seq_to = _max_seq(client)
    return client.post(
        "/reconciliations",
        json={
            "batch_no": batch_no,
            "channel": channel,
            "from_entry_seq": seq_from,
            "to_entry_seq": seq_to,
            "receipts": receipts if receipts is not None else [],
        },
    )


def _receipt(no, kind, ref_id, copies, price):
    return {"receipt_no": no, "type": kind, "ref_id": ref_id, "copies": copies, "net_price": price}


def _line_map(body):
    return {(ln["type"], ln["ref_id"]): ln for ln in body["lines"]}


# ------------------------------------------------------------ 基本四类结论


def test_all_four_statuses_and_summaries(client):
    _work(client)
    _sale(client, "S1", copies=10, price="33.33")
    _sale(client, "S2", copies=5, price="20.00")
    _refund(client, "R1", "S1", copies=2)
    receipts = [
        _receipt("P1", "sale", "S1", 10, "33.33"),      # 一致
        _receipt("P2", "sale", "S2", 4, "20.00"),       # 册数不符
        _receipt("P3", "refund", "R1", 2, "33.33"),     # 一致（退款取原销售入账价）
        _receipt("P4", "sale", "S99", 1, "10.00"),      # 渠道多报：内部无此编号
    ]
    r = _recon(client, "RC-1", 0, 3, receipts)
    assert r.status_code == 201
    body = r.json()
    assert body["counts"] == {
        "receipts": 4,
        "internal": 3,
        "matched": 2,
        "value_mismatch": 1,
        "channel_extra": 1,
        "internal_missing": 0,
    }
    lm = _line_map(body)
    assert lm[("sale", "S1")]["status"] == "matched"
    assert lm[("sale", "S1")]["copies_match"] and lm[("sale", "S1")]["net_price_match"]
    s2 = lm[("sale", "S2")]
    assert s2["status"] == "value_mismatch"
    assert s2["copies_match"] is False and s2["net_price_match"] is True
    assert s2["channel"]["copies"] == 4 and s2["internal"]["copies"] == 5
    assert lm[("refund", "R1")]["status"] == "matched"
    assert lm[("sale", "S99")]["status"] == "channel_extra"
    assert lm[("sale", "S99")]["reason"] == "not_found"
    # 行排序：内部基准按入账序号，渠道多报追加在后
    statuses = [ln["status"] for ln in body["lines"]]
    assert statuses == ["matched", "value_mismatch", "matched", "channel_extra"]
    assert [ln["seq"] for ln in body["lines"]] == [1, 2, 3, 4]
    # 双方汇总
    assert body["channel_summary"]["sales"] == {"count": 3, "copies": 15, "amount_cents": 42330}
    assert body["channel_summary"]["refunds"] == {"count": 1, "copies": 2, "amount_cents": 6666}
    assert body["channel_summary"]["net_amount_cents"] == 42330 - 6666
    assert body["internal_summary"]["sales"] == {"count": 2, "copies": 15, "amount_cents": 43330}
    assert body["internal_summary"]["refunds"] == {"count": 1, "copies": 2, "amount_cents": 6666}
    assert body["internal_summary"]["net_amount_cents"] == 43330 - 6666


def test_internal_missing_with_empty_receipt_list(client):
    _work(client)
    _sale(client, "S1")
    r = _recon(client, "RC-1", 0, 1, [])
    assert r.status_code == 201
    body = r.json()
    assert body["counts"]["internal_missing"] == 1
    ln = body["lines"][0]
    assert ln["status"] == "internal_missing"
    assert ln["channel"] is None
    assert ln["internal"]["net_price"] == "33.33"
    assert body["channel_summary"]["sales"]["count"] == 0
    assert body["internal_summary"]["sales"]["count"] == 1


def test_net_price_mismatch(client):
    _work(client)
    _sale(client, "S1", copies=3, price="33.33")
    r = _recon(client, "RC-1", 0, 1, [_receipt("P1", "sale", "S1", 3, "30.00")])
    ln = _line_map(r.json())[("sale", "S1")]
    assert ln["status"] == "value_mismatch"
    assert ln["copies_match"] is True and ln["net_price_match"] is False
    assert ln["channel"]["net_price"] == "30" and ln["internal"]["net_price"] == "33.33"


def test_refund_uses_original_sale_price_after_correction(client):
    _work(client)
    _sale(client, "S1", copies=10, price="33.33")
    _refund(client, "R1", "S1", 1)
    client.post(
        "/price-corrections",
        json={"correction_id": "C1", "sale_id": "S1", "new_net_price": "40.00"},
    )
    _refund(client, "R2", "S1", 1)  # seq: S1=1 R1=2 C1=3 R2=4 reversal=5
    # 区间 (2,5]：只有更正 C1、退款 R2 与反向流水；后两者不参与，内部仅 R2
    r = _recon(
        client,
        "RC-1",
        2,
        5,
        [_receipt("P1", "refund", "R2", 1, "33.33")],  # 渠道按原销售价回单 → 一致
    )
    assert r.status_code == 201
    body = r.json()
    assert body["counts"]["internal"] == 1
    ln = _line_map(body)[("refund", "R2")]
    assert ln["status"] == "matched"
    assert ln["internal"]["net_price"] == "33.33"  # 不是更正后的 40


def test_channel_extra_reasons(client):
    _work(client)
    _sale(client, "S1")  # seq 1
    _refund(client, "R1", "S1", 1)  # seq 2
    receipts = [
        _receipt("P0", "refund", "R1", 1, "33.33"),     # 区间内退款：一致
        _receipt("P1", "refund", "S1", 1, "33.33"),     # 编号存在但类型不符
        _receipt("P2", "sale", "S1", 10, "33.33"),      # 同类型在区间 (1,2] 之外
        _receipt("P3", "sale", "NOPE", 1, "1.00"),      # 内部完全无此编号
    ]
    r = _recon(client, "RC-1", 1, 2, receipts)
    assert r.status_code == 201
    lm = _line_map(r.json())
    assert lm[("refund", "R1")]["status"] == "matched"
    assert lm[("refund", "S1")]["status"] == "channel_extra"
    assert lm[("refund", "S1")]["reason"] == "kind_mismatch"
    assert lm[("sale", "S1")]["reason"] == "not_in_interval"
    assert lm[("sale", "NOPE")]["reason"] == "not_found"
    assert sum(1 for ln in r.json()["lines"] if ln["status"] == "channel_extra") == 3


def test_sale_and_refund_with_same_id_are_distinct(client):
    _work(client)
    _sale(client, "X1", copies=1, price="10.00")
    # 造一条退款编号恰与另一销售同号（跨类型编号空间独立）
    _sale(client, "S2", copies=2, price="10.00")
    _refund(client, "S2", "S2", 1)  # 合法：退款号 S2 指向销售 S2
    r = _recon(
        client,
        "RC-1",
        0,
        3,
        [
            _receipt("P1", "sale", "X1", 1, "10.00"),
            _receipt("P2", "sale", "S2", 2, "10.00"),
            _receipt("P3", "refund", "S2", 1, "10.00"),
        ],
    )
    assert r.status_code == 201
    assert all(ln["status"] == "matched" for ln in r.json()["lines"])


# ------------------------------------------------------------ 空回单（copies=0）


def test_empty_receipt_copies_zero_checks_omission(client):
    _work(client)
    _sale(client, "S1", copies=5, price="10.00")
    # 空回单（0 册）引用 S1：参与核对，数值不符 → value_mismatch（册数不符），不算漏报也不算多报
    r = _recon(client, "RC-1", 0, 1, [_receipt("P0", "sale", "S1", 0, "0.00")])
    body = r.json()
    ln = _line_map(body)[("sale", "S1")]
    assert ln["status"] == "value_mismatch"
    assert ln["copies_match"] is False
    assert ln["channel"]["amount_cents"] == 0
    assert body["counts"]["internal_missing"] == 0
    assert body["channel_summary"]["sales"]["copies"] == 0


# ------------------------------------------------------------ 整批拒绝


def test_duplicate_receipt_no_rejects_batch(client):
    _work(client)
    _sale(client, "S1")
    _sale(client, "S2")
    r = _recon(
        client,
        "RC-1",
        0,
        2,
        [
            _receipt("P1", "sale", "S1", 10, "33.33"),
            _receipt("P1", "sale", "S2", 10, "33.33"),
        ],
    )
    assert r.status_code == 400
    assert "回单号批内重复" in r.json()["detail"]
    # 整批拒绝不落库
    assert client.get("/reconciliations").json()["reconciliations"] == []


def test_duplicate_internal_ref_rejects_batch(client):
    _work(client)
    _sale(client, "S1")
    r = _recon(
        client,
        "RC-1",
        0,
        1,
        [
            _receipt("P1", "sale", "S1", 10, "33.33"),
            _receipt("P2", "sale", "S1", 10, "33.33"),
        ],
    )
    assert r.status_code == 400
    assert "同一内部流水出现多次" in r.json()["detail"]


def test_interval_out_of_bounds_rejects_batch(client):
    _work(client)
    _sale(client, "S1")  # max entry_seq = 1
    r = _recon(client, "RC-1", 0, 2, [])
    assert r.status_code == 400
    assert "区间越界" in r.json()["detail"]
    # 空库对账（max=0）也越界
    db.open_database(":memory:")
    db.init_db()
    with TestClient(main.app) as c2:
        r2 = _recon(c2, "RC-1", 0, 1, [])
        assert r2.status_code == 400


def test_interval_must_be_positive_length(client):
    _work(client)
    _sale(client, "S1")
    assert _recon(client, "RC-1", 1, 1, []).status_code == 400  # 零长度区间
    assert _recon(client, "RC-2", 2, 1, []).status_code == 400  # 反向区间（整批拒绝）
    assert _recon(client, "RC-3", -1, 1, []).status_code == 422  # 负起点（schema 拦截）


def test_rejected_batch_can_resubmit_same_batch_no_after_fix(client):
    _work(client)
    _sale(client, "S1")
    bad = _recon(
        client, "RC-1", 0, 1,
        [_receipt("P1", "sale", "S1", 10, "33.33"), _receipt("P1", "sale", "S1", 9, "33.33")],
    )
    assert bad.status_code == 400
    ok = _recon(client, "RC-1", 0, 1, [_receipt("P1", "sale", "S1", 10, "33.33")])
    assert ok.status_code == 201


def test_invalid_numbers_422(client):
    body = {
        "batch_no": "RC-1",
        "channel": "CH-X",
        "from_entry_seq": 0,
        "to_entry_seq": 1,
        "receipts": [
            {"receipt_no": "P1", "type": "sale", "ref_id": "S1", "copies": -1, "net_price": 1}
        ],
    }
    assert client.post("/reconciliations", json=body).status_code == 422
    body["receipts"][0]["copies"] = 1
    body["receipts"][0]["net_price"] = -0.01
    assert client.post("/reconciliations", json=body).status_code == 422
    body["receipts"][0]["net_price"] = 10
    body["receipts"][0]["type"] = "chargeback"
    assert client.post("/reconciliations", json=body).status_code == 422


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_decimal_returns_clean_422(client, token):
    raw = (
        '{"batch_no":"RC-1","channel":"CH-X","from_entry_seq":0,"to_entry_seq":1,'
        f'"receipts":[{{"receipt_no":"P1","type":"sale","ref_id":"S1",'
        f'"copies":1,"net_price":{token}}}]}}'
    )
    r = client.post(
        "/reconciliations", content=raw, headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 422
    # 错误响应本身必须是合法 JSON（NaN/Infinity 不得导致 500）
    assert isinstance(r.json()["detail"], list)


def test_chinese_type_alias(client):
    _work(client)
    _sale(client, "S1")
    r = _recon(client, "RC-1", 0, 1, [_receipt("P1", "销售", "S1", 10, "33.33")])
    assert r.status_code == 201
    assert _line_map(r.json())[("sale", "S1")]["status"] == "matched"


# ------------------------------------------------------------ 幂等、冻结、快照


def test_replay_returns_frozen_report_200(client):
    _work(client)
    _sale(client, "S1")
    payload = {
        "batch_no": "RC-1",
        "channel": "CH-X",
        "from_entry_seq": 0,
        "to_entry_seq": 1,
        "receipts": [_receipt("P1", "sale", "S1", 10, "33.33")],
    }
    r1 = client.post("/reconciliations", json=payload)
    assert r1.status_code == 201 and r1.json()["reconciliation_id"] == 1
    r2 = client.post("/reconciliations", json=payload)
    assert r2.status_code == 200
    assert r2.json() == r1.json()


def test_replay_order_insensitive(client):
    _work(client)
    _sale(client, "S1")
    _sale(client, "S2")
    base = {
        "batch_no": "RC-1",
        "channel": "CH-X",
        "from_entry_seq": 0,
        "to_entry_seq": 2,
        "receipts": [
            _receipt("P1", "sale", "S1", 10, "33.33"),
            _receipt("P2", "sale", "S2", 10, "33.33"),
        ],
    }
    import copy

    swapped = copy.deepcopy(base)
    swapped["receipts"] = list(reversed(swapped["receipts"]))
    assert client.post("/reconciliations", json=base).status_code == 201
    assert client.post("/reconciliations", json=swapped).status_code == 200


def test_same_batch_no_different_content_conflict(client):
    _work(client)
    _sale(client, "S1")
    p = {
        "batch_no": "RC-1",
        "channel": "CH-X",
        "from_entry_seq": 0,
        "to_entry_seq": 1,
        "receipts": [_receipt("P1", "sale", "S1", 10, "33.33")],
    }
    assert client.post("/reconciliations", json=p).status_code == 201
    # 渠道不同 / 回单内容不同 均为异内容冲突
    p2 = {
        "batch_no": "RC-1",
        "channel": "CH-Y",
        "from_entry_seq": 0,
        "to_entry_seq": 1,
        "receipts": [_receipt("P1", "sale", "S1", 10, "33.33")],
    }
    assert client.post("/reconciliations", json=p2).status_code == 409
    p3 = {
        "batch_no": "RC-1",
        "channel": "CH-X",
        "from_entry_seq": 0,
        "to_entry_seq": 1,
        "receipts": [_receipt("P1", "sale", "S1", 9, "33.33")],
    }
    assert client.post("/reconciliations", json=p3).status_code == 409


def test_report_frozen_after_later_entries(client):
    _work(client)
    _sale(client, "S1", copies=10)
    r1 = _recon(client, "RC-1", 0, 1, [_receipt("P1", "sale", "S1", 10, "33.33")])
    assert r1.status_code == 201
    # 后续补录、退款、更正都不改变旧报告
    _sale(client, "S2", copies=99, price="55.00", ts="2026-08-01T00:00:00")
    _refund(client, "R1", "S1", 10)
    client.post(
        "/price-corrections",
        json={"correction_id": "C1", "sale_id": "S1", "new_net_price": "88.00"},
    )
    frozen = client.get("/reconciliations/1").json()
    assert frozen == r1.json()
    assert frozen["counts"]["internal"] == 1
    assert client.get("/reconciliations/by-batch/RC-1").json() == r1.json()


def test_reconciliation_does_not_mutate_existing_data(client):
    _work(client)
    _sale(client, "S1", copies=10, price="33.33")
    _refund(client, "R1", "S1", 2)
    sale_before = client.get("/sales/S1").json()
    refund_before = client.get("/refunds/R1").json()
    _recon(
        client,
        "RC-1",
        0,
        2,
        [
            _receipt("P1", "sale", "S1", 9, "33.33"),
            _receipt("P2", "refund", "R1", 2, "33.33"),
        ],
    )
    assert client.get("/sales/S1").json() == sale_before
    assert client.get("/refunds/R1").json() == refund_before
    # 关账与付款不受对账影响、且对账不占用入账序号
    close = client.post("/closes", json={"request_id": "CL-1"}).json()
    assert close["entry_seq_from"] == 1 and close["entry_seq_to"] == 2


def test_list_and_filter_and_404(client):
    _work(client)
    _sale(client, "S1")
    _recon(client, "RC-1", 0, 1, [_receipt("P1", "sale", "S1", 10, "33.33")], channel="CH-X")
    _recon(client, "RC-2", 0, 1, [], channel="CH-Y")
    lst = client.get("/reconciliations").json()["reconciliations"]
    assert [r["batch_no"] for r in lst] == ["RC-1", "RC-2"]
    only_x = client.get("/reconciliations", params={"channel": "CH-X"}).json()
    assert [r["batch_no"] for r in only_x["reconciliations"]] == ["RC-1"]
    assert client.get("/reconciliations/999").status_code == 404
    assert client.get("/reconciliations/by-batch/NOPE").status_code == 404


def test_partial_interval_window(client):
    _work(client)
    _sale(client, "S1")  # seq 1
    _sale(client, "S2")  # seq 2
    _sale(client, "S3")  # seq 3
    # 区间 (1,3] 只含 S2、S3
    r = _recon(
        client,
        "RC-1",
        1,
        3,
        [_receipt("P1", "sale", "S2", 10, "33.33")],
    )
    body = r.json()
    refs = {ln["ref_id"] for ln in body["lines"]}
    assert refs == {"S2", "S3"}
    assert _line_map(body)[("sale", "S3")]["status"] == "internal_missing"
    # 回单引用区间外的 S1（同类型存在）→ 渠道多报，原因 not_in_interval
    r2 = _recon(
        client,
        "RC-2",
        1,
        3,
        [
            _receipt("P1", "sale", "S2", 10, "33.33"),
            _receipt("P2", "sale", "S3", 10, "33.33"),
            _receipt("P3", "sale", "S1", 10, "33.33"),
        ],
    )
    assert _line_map(r2.json())[("sale", "S1")]["reason"] == "not_in_interval"


# ------------------------------------------------------------ 并发


def test_concurrent_batches_serialized(client):
    _work(client)
    _sale(client, "S1")
    results = []

    def submit(batch_no):
        r = _recon(
            client,
            batch_no,
            0,
            1,
            [_receipt(f"P-{batch_no}", "sale", "S1", 10, "33.33")],
        )
        results.append(r.status_code)

    threads = [threading.Thread(target=submit, args=(f"RC-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [201] * 8
    assert len(client.get("/reconciliations").json()["reconciliations"]) == 8
