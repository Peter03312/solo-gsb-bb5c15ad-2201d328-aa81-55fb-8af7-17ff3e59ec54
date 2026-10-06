"""渠道对账：财务以批次号 + 入账序号区间 (from, to] + 渠道逐笔回单提交核对。

业务规则：
- 区间内只取**原销售与原退款**两种内部流水：净价更正（price_correction）与
  价差反向流水（price_delta_reversal）不参与；
- 退款的内部净价一律取**原销售入账价**（与后续净价更正无关），册数取退款册数；
- 逐笔结论：
    matched           一致（册数、每册净价均相符）
    value_mismatch    册数或净价不符（按 (类型, 内部流水编号) 对上但数值不一致）
    channel_extra     渠道多报（回单在内部销售/退款中找不到同 (类型,编号) 的区间内流水）
    internal_missing  内部漏报（区间内有内部流水但渠道未提交对应回单）
  报告含双方原值（回单侧/内部侧）与双方各自汇总（销售/退款册数与金额、净额）；
- 空回单（copies=0，仍带类型/编号/净价）合法，按编号参与核对以核查漏报；
  整批回单列表为空亦合法（全部内部流水落入内部漏报）；
- 整批拒绝（不落库、不生成报告）：回单号批内重复、同一内部流水 (type, ref_id)
  出现多次、区间非法或终点越界（超过当前最大入账序号）；请求体数值非法 → 422；
- 核对与保存在**同一数据快照**中完成：持全局锁、单事务读取区间快照并写入冻结报告；
  成功后报告不随后续补录/退款/更正改变；
- 同批次号同内容（回单顺序无关）重试返回原冻结报告（HTTP 200），异内容 → 409；
- 对账只**读取**销售/退款数据并写入 reconciliations 表，不改写销售、退款、关账、付款。
"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, ROUND_HALF_UP

from .db import LOCK, get_conn
from .ledger import ApiError, _fingerprint, _utcnow, dec_str

CENT = Decimal("0.01")

TYPE_LABELS = {"sale": "销售", "refund": "退款"}


def _bad_request(msg: str) -> ApiError:
    return ApiError(400, msg)


def _not_found(msg: str) -> ApiError:
    return ApiError(404, msg)


def _conflict(msg: str) -> ApiError:
    return ApiError(409, msg)


def yuan_to_cents(price: Decimal, copies: int) -> int:
    """册数 × 每册净价对应的金额（分）：单价四舍五入到分后乘册数。"""
    per_copy = int((price.quantize(CENT, rounding=ROUND_HALF_UP)) * 100)
    return per_copy * copies


def _receipt_fingerprint(p) -> str:
    # 回单按回单号排序后参与指纹：同一批回单不同提交顺序视为同内容
    receipts = sorted(p.receipts, key=lambda r: r.receipt_no)
    return _fingerprint(
        {
            "batch_no": p.batch_no,
            "channel": p.channel,
            "from_entry_seq": p.from_entry_seq,
            "to_entry_seq": p.to_entry_seq,
            "receipts": [
                {
                    "receipt_no": r.receipt_no,
                    "type": r.type,
                    "ref_id": r.ref_id,
                    "copies": r.copies,
                    "net_price": dec_str(r.net_price),
                }
                for r in receipts
            ],
        }
    )


def _empty_kind_summary() -> dict:
    return {"count": 0, "copies": 0, "amount_cents": 0}


def _add(summary: dict, copies: int, amount_cents: int) -> None:
    summary["count"] += 1
    summary["copies"] += copies
    summary["amount_cents"] += amount_cents


def _internal_rows(conn: sqlite3.Connection, seq_from: int, seq_to: int) -> list[dict]:
    """区间 (seq_from, seq_to] 内的原销售/原退款流水（其余流水类型不参与对账）。

    退款的内部净价取原销售入账价 sales.net_price（不受净价更正影响）。
    """
    rows = conn.execute(
        "SELECT s.entry_seq AS entry_seq, 'sale' AS kind, s.sale_id AS ref_id,"
        " s.copies AS copies, s.net_price AS net_price FROM sales s"
        " WHERE s.entry_seq>? AND s.entry_seq<=?"
        " UNION ALL"
        " SELECT r.entry_seq AS entry_seq, 'refund' AS kind, r.refund_id AS ref_id,"
        " r.copies AS copies, s.net_price AS net_price FROM refunds r"
        " JOIN sales s ON s.sale_id=r.sale_id"
        " WHERE r.entry_seq>? AND r.entry_seq<=?"
        " ORDER BY entry_seq",
        (seq_from, seq_to, seq_from, seq_to),
    ).fetchall()
    return [dict(r) for r in rows]


def _locate_ref(
    conn: sqlite3.Connection, kind: str, ref_id: str
) -> tuple[str | None, int | None]:
    """回单引用的内部流水定位：返回 (实际类型, 入账序号)；销售/退款均无则 (None, None)。"""
    if kind == "sale":
        r = conn.execute(
            "SELECT entry_seq FROM sales WHERE sale_id=?", (ref_id,)
        ).fetchone()
        if r is not None:
            return "sale", r["entry_seq"]
        r = conn.execute(
            "SELECT r.entry_seq AS entry_seq FROM refunds r WHERE r.refund_id=?",
            (ref_id,),
        ).fetchone()
        return ("refund", r["entry_seq"]) if r is not None else (None, None)
    r = conn.execute(
        "SELECT entry_seq FROM refunds WHERE refund_id=?", (ref_id,)
    ).fetchone()
    if r is not None:
        return "refund", r["entry_seq"]
    r = conn.execute(
        "SELECT entry_seq FROM sales WHERE sale_id=?", (ref_id,)
    ).fetchone()
    return ("sale", r["entry_seq"]) if r is not None else (None, None)


def _build_report(conn: sqlite3.Connection, p, reconciliation_id: int, created_at: str) -> dict:
    """在已确定的区间快照上完成全部核对，返回冻结报告（dict）。调用方持锁、在事务内。"""
    seq_from, seq_to = p.from_entry_seq, p.to_entry_seq
    internal_rows = _internal_rows(conn, seq_from, seq_to)
    internal_map: dict[tuple[str, str], dict] = {
        (r["kind"], r["ref_id"]): r for r in internal_rows
    }

    lines: list[dict] = []
    channel_summary = {"sales": _empty_kind_summary(), "refunds": _empty_kind_summary()}
    internal_summary = {"sales": _empty_kind_summary(), "refunds": _empty_kind_summary()}
    counts = {"matched": 0, "value_mismatch": 0, "channel_extra": 0, "internal_missing": 0}

    # 1) 以内部区间流水为基准：找到回单则逐笔比对，否则为内部漏报
    for row in internal_rows:
        kind, ref_id = row["kind"], row["ref_id"]
        int_price = Decimal(row["net_price"])
        int_amount = yuan_to_cents(int_price, row["copies"])
        _add(internal_summary[kind + "s"], row["copies"], int_amount)
        internal_side = {
            "entry_seq": row["entry_seq"],
            "copies": row["copies"],
            "net_price": dec_str(int_price),
            "amount_cents": int_amount,
        }
        receipt = next(
            (r for r in p.receipts if r.type == kind and r.ref_id == ref_id), None
        )
        if receipt is None:
            counts["internal_missing"] += 1
            lines.append(
                {
                    "status": "internal_missing",
                    "type": kind,
                    "type_label": TYPE_LABELS[kind],
                    "receipt_no": None,
                    "ref_id": ref_id,
                    "entry_seq": row["entry_seq"],
                    "copies_match": None,
                    "net_price_match": None,
                    "reason": None,
                    "channel": None,
                    "internal": internal_side,
                }
            )
            continue
        ch_amount = yuan_to_cents(receipt.net_price, receipt.copies)
        copies_match = receipt.copies == row["copies"]
        net_price_match = receipt.net_price == int_price
        status = "matched" if copies_match and net_price_match else "value_mismatch"
        counts[status] += 1
        _add(channel_summary[kind + "s"], receipt.copies, ch_amount)
        lines.append(
            {
                "status": status,
                "type": kind,
                "type_label": TYPE_LABELS[kind],
                "receipt_no": receipt.receipt_no,
                "ref_id": ref_id,
                "entry_seq": row["entry_seq"],
                "copies_match": copies_match,
                "net_price_match": net_price_match,
                "reason": None,
                "channel": {
                    "receipt_no": receipt.receipt_no,
                    "copies": receipt.copies,
                    "net_price": dec_str(receipt.net_price),
                    "amount_cents": ch_amount,
                },
                "internal": internal_side,
            }
        )

    # 2) 回单在区间内无同 (类型,编号) 内部流水 → 渠道多报；附定位原因
    for receipt in p.receipts:
        key = (receipt.type, receipt.ref_id)
        if key in internal_map:
            continue
        ch_amount = yuan_to_cents(receipt.net_price, receipt.copies)
        _add(channel_summary[receipt.type + "s"], receipt.copies, ch_amount)
        actual_kind, actual_seq = _locate_ref(conn, receipt.type, receipt.ref_id)
        if actual_kind is None:
            reason = "not_found"  # 内部无此销售/退款编号
            entry_seq = None
        elif actual_kind != receipt.type:
            reason = "kind_mismatch"  # 编号存在但类型不符（回单称销售、内部为退款等）
            entry_seq = actual_seq
        elif not (seq_from < actual_seq <= seq_to):
            reason = "not_in_interval"  # 同类型流水存在但入账序号越出本批区间
            entry_seq = actual_seq
        else:  # 理论不可达：同类型且在区间内应已进入 internal_map
            reason = "not_found"
            entry_seq = None
        counts["channel_extra"] += 1
        lines.append(
            {
                "status": "channel_extra",
                "type": receipt.type,
                "type_label": TYPE_LABELS[receipt.type],
                "receipt_no": receipt.receipt_no,
                "ref_id": receipt.ref_id,
                "entry_seq": entry_seq,
                "copies_match": None,
                "net_price_match": None,
                "reason": reason,
                "channel": {
                    "receipt_no": receipt.receipt_no,
                    "copies": receipt.copies,
                    "net_price": dec_str(receipt.net_price),
                    "amount_cents": ch_amount,
                },
                "internal": None,
            }
        )

    # 排序：内部流水基准行按入账序号升序，渠道多报行按回单提交顺序追加在后
    internal_lines = [ln for ln in lines if ln["status"] != "channel_extra"]
    extra_lines = [ln for ln in lines if ln["status"] == "channel_extra"]
    internal_lines.sort(key=lambda ln: ln["entry_seq"])
    ordered = internal_lines + extra_lines
    for i, ln in enumerate(ordered, start=1):
        ln["seq"] = i
    # 统一字段顺序
    lines = [
        {
            "seq": ln["seq"],
            "status": ln["status"],
            "type": ln["type"],
            "type_label": ln["type_label"],
            "receipt_no": ln["receipt_no"],
            "ref_id": ln["ref_id"],
            "entry_seq": ln["entry_seq"],
            "copies_match": ln["copies_match"],
            "net_price_match": ln["net_price_match"],
            "reason": ln["reason"],
            "channel": ln["channel"],
            "internal": ln["internal"],
        }
        for ln in ordered
    ]

    def _with_net(summary: dict) -> dict:
        return {
            "sales": summary["sales"],
            "refunds": summary["refunds"],
            "net_amount_cents": summary["sales"]["amount_cents"]
            - summary["refunds"]["amount_cents"],
        }

    max_seq = conn.execute(
        "SELECT COALESCE(MAX(entry_seq),0) AS c FROM entries"
    ).fetchone()["c"]
    return {
        "reconciliation_id": reconciliation_id,
        "batch_no": p.batch_no,
        "channel": p.channel,
        "from_entry_seq": seq_from,
        "to_entry_seq": seq_to,
        "entry_seq_from": seq_from + 1,
        "entry_seq_to": seq_to,
        "snapshot_max_entry_seq": max_seq,
        "created_at": created_at,
        "counts": {
            "receipts": len(p.receipts),
            "internal": len(internal_rows),
            "matched": counts["matched"],
            "value_mismatch": counts["value_mismatch"],
            "channel_extra": counts["channel_extra"],
            "internal_missing": counts["internal_missing"],
        },
        "lines": lines,
        "channel_summary": _with_net(channel_summary),
        "internal_summary": _with_net(internal_summary),
    }


def submit_reconciliation(p) -> tuple[dict, bool]:
    """提交渠道对账。返回 (冻结报告, 是否新建)。"""
    fp = _receipt_fingerprint(p)
    with LOCK:
        conn = get_conn()
        # 幂等优先：同批次号同内容重试返回原冻结报告
        row = conn.execute(
            "SELECT fingerprint, report FROM reconciliations WHERE batch_no=?",
            (p.batch_no,),
        ).fetchone()
        if row is not None:
            if row["fingerprint"] != fp:
                raise _conflict(f"对账批次号 {p.batch_no} 已存在且提交内容不一致")
            return json.loads(row["report"]), False

        # 整批拒绝类校验（请求体格式/数值非法已由 pydantic 拦截为 422）
        reasons: list[str] = []
        if p.to_entry_seq <= p.from_entry_seq:
            reasons.append(
                f"入账序号区间非法：({p.from_entry_seq}, {p.to_entry_seq}] 必须为正整数长度"
            )
        seen_receipt_no: set[str] = set()
        dup_receipt_no: list[str] = []
        seen_keys: dict[tuple[str, str], str] = {}
        dup_keys: list[str] = []
        for r in p.receipts:
            if r.receipt_no in seen_receipt_no and r.receipt_no not in dup_receipt_no:
                dup_receipt_no.append(r.receipt_no)
            seen_receipt_no.add(r.receipt_no)
            key = (r.type, r.ref_id)
            if key in seen_keys and r.ref_id not in dup_keys:
                dup_keys.append(r.ref_id)
            seen_keys.setdefault(key, r.receipt_no)
        if dup_receipt_no:
            reasons.append(f"回单号批内重复：{', '.join(dup_receipt_no)}")
        if dup_keys:
            reasons.append(f"同一内部流水出现多次：{', '.join(dup_keys)}")

        conn.execute("BEGIN IMMEDIATE")
        try:
            max_seq = conn.execute(
                "SELECT COALESCE(MAX(entry_seq),0) AS c FROM entries"
            ).fetchone()["c"]
            if p.to_entry_seq > max_seq:
                reasons.append(
                    f"区间终点 {p.to_entry_seq} 超出当前最大入账序号 {max_seq}（区间越界）"
                )
            if reasons:
                conn.rollback()
                raise _bad_request("整批拒绝：" + "；".join(reasons))

            created_at = _utcnow()
            # 先在同一快照事务内完成全部核对，再落库冻结报告：核对与保存基于同一数据快照
            cur = conn.execute(
                "INSERT INTO reconciliations(batch_no, channel, from_entry_seq,"
                " to_entry_seq, max_entry_seq, receipt_count, internal_count,"
                " matched_count, value_mismatch_count, channel_extra_count,"
                " internal_missing_count, fingerprint, report, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    p.batch_no, p.channel, p.from_entry_seq, p.to_entry_seq, max_seq,
                    0, 0, 0, 0, 0, 0, fp, "", created_at,
                ),
            )
            report = _build_report(conn, p, cur.lastrowid, created_at)
            c = report["counts"]
            conn.execute(
                "UPDATE reconciliations SET receipt_count=?, internal_count=?,"
                " matched_count=?, value_mismatch_count=?, channel_extra_count=?,"
                " internal_missing_count=?, max_entry_seq=?, report=?"
                " WHERE reconciliation_id=?",
                (
                    c["receipts"], c["internal"], c["matched"], c["value_mismatch"],
                    c["channel_extra"], c["internal_missing"],
                    report["snapshot_max_entry_seq"],
                    json.dumps(report, ensure_ascii=False), cur.lastrowid,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return report, True


def _report_from_row(row: sqlite3.Row) -> dict:
    return json.loads(row["report"])


def get_reconciliation(reconciliation_id: int) -> dict:
    with LOCK:
        row = get_conn().execute(
            "SELECT report FROM reconciliations WHERE reconciliation_id=?",
            (reconciliation_id,),
        ).fetchone()
        if row is None:
            raise _not_found(f"对账批次 {reconciliation_id} 不存在")
        return _report_from_row(row)


def get_reconciliation_by_batch(batch_no: str) -> dict:
    with LOCK:
        row = get_conn().execute(
            "SELECT report FROM reconciliations WHERE batch_no=?", (batch_no,)
        ).fetchone()
        if row is None:
            raise _not_found(f"对账批次号 {batch_no} 不存在")
        return _report_from_row(row)


def list_reconciliations(channel: str | None = None) -> dict:
    """对账批次列表（冻结报告概览，可按渠道过滤）。"""
    with LOCK:
        conn = get_conn()
        if channel is None:
            rows = conn.execute(
                "SELECT * FROM reconciliations ORDER BY reconciliation_id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reconciliations WHERE channel=? ORDER BY reconciliation_id",
                (channel,),
            ).fetchall()
        return {
            "reconciliations": [
                {
                    "reconciliation_id": r["reconciliation_id"],
                    "batch_no": r["batch_no"],
                    "channel": r["channel"],
                    "from_entry_seq": r["from_entry_seq"],
                    "to_entry_seq": r["to_entry_seq"],
                    "entry_seq_from": r["from_entry_seq"] + 1,
                    "entry_seq_to": r["to_entry_seq"],
                    "snapshot_max_entry_seq": r["max_entry_seq"],
                    "receipt_count": r["receipt_count"],
                    "internal_count": r["internal_count"],
                    "matched_count": r["matched_count"],
                    "value_mismatch_count": r["value_mismatch_count"],
                    "channel_extra_count": r["channel_extra_count"],
                    "internal_missing_count": r["internal_missing_count"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        }
