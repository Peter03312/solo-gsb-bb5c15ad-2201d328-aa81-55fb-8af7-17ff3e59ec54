#!/usr/bin/env bash
# 端到端调用示例：录入作品 → 销售 → 退款 → 关账 → 查看报表 → 付款结算
#              → 作者分成版本变更 → 新版本销售/旧销售退款 → 退出作者结算
#              → 净价更正 → 渠道对账 → 付款冲回（银行退票）→ 作品关账贡献查询
#              → 待付额度预约（预约/直接付款扣占额/执行/取消/新关账失效/退票/错误码）
# 用法：BASE_URL=http://localhost:8000 ./examples/demo.sh
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
fmt() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 1. 录入作品（两档阶梯 + 两位作者 60/40 分成）=="
curl -s -X POST "$BASE/works" -H 'Content-Type: application/json' -d '{
  "work_id": "BOOK-1",
  "title": "示例作品",
  "tiers": [
    {"threshold": 0,    "rate": 0.08},
    {"threshold": 1000, "rate": 0.10}
  ],
  "authors": [
    {"author_id": "A1", "ratio": 60},
    {"author_id": "A2", "ratio": 40}
  ]
}' | fmt

echo "== 2. 登记销售 1200 册（跨档：1000 册 8% + 200 册 10%）=="
curl -s -X POST "$BASE/sales" -H 'Content-Type: application/json' -d '{
  "sale_id": "S100",
  "work_id": "BOOK-1",
  "business_time": "2026-09-01T10:00:00",
  "copies": 1200,
  "net_price": 33.33
}' | fmt

echo "== 3. 退款 250 册（从最后一册起冲回：200 册 10% 档 + 50 册 8% 档）=="
curl -s -X POST "$BASE/refunds" -H 'Content-Type: application/json' -d '{
  "refund_id": "RF100",
  "sale_id": "S100",
  "copies": 250
}' | fmt

echo "== 4. 关账，生成不可改报表 =="
curl -s -X POST "$BASE/closes" -H 'Content-Type: application/json' -d '{
  "request_id": "CLOSE-2026-09"
}' | fmt

echo "== 5. 关账后补录旧业务流水（计入下一批）=="
curl -s -X POST "$BASE/sales" -H 'Content-Type: application/json' -d '{
  "sale_id": "S101",
  "work_id": "BOOK-1",
  "business_time": "2026-08-15T08:00:00",
  "copies": 5,
  "net_price": 20.00
}' | fmt

echo "== 6. 再次关账并查看两批报表 =="
curl -s -X POST "$BASE/closes" | fmt
curl -s "$BASE/closes" | fmt

echo "== 7. 财务确认作者付款（针对当前最新批次，即第 2 批）=="
LATEST_CLOSE=$(curl -s "$BASE/authors/A1/settlement" | python3 -c 'import sys,json; print(json.load(sys.stdin)["latest_close_id"])')
echo "当前最新关账批次：$LATEST_CLOSE"
# A1 第1批净额 = 200160（销售）- 47970（退款冲回）= 152190
# A1 第2批净额 = 5 册×200 分(10% 档)×60% = 600；可付额 = 152190 + 600 = 152790 分
curl -s -X POST "$BASE/payments" -H 'Content-Type: application/json' -d "{
  \"payment_id\": \"PAY-2026-09-A1\",
  \"author_id\": \"A1\",
  \"close_id\": $LATEST_CLOSE,
  \"amount_cents\": 100000
}" | fmt

echo "== 8. 同付款编号同内容重试（返回原结算结果，即使之后再关账也不变）=="
curl -s -X POST "$BASE/payments" -H 'Content-Type: application/json' -d "{
  \"payment_id\": \"PAY-2026-09-A1\",
  \"author_id\": \"A1\",
  \"close_id\": $LATEST_CLOSE,
  \"amount_cents\": 100000
}" | fmt

echo "== 9. 按作者查询：分批应计/已付/负余额/可付额/付款记录 =="
curl -s "$BASE/authors/A1/settlement" | fmt
curl -s "$BASE/payments/PAY-2026-09-A1" | fmt

echo "== 10. 合作关系变化：提交作者分成新版本（A2 退出、A3 加入；变更编号全局唯一）=="
curl -s -X POST "$BASE/works/BOOK-1/splits" -H 'Content-Type: application/json' -d '{
  "change_id": "CHG-2026-10-01",
  "authors": [
    {"author_id": "A1", "ratio": 70},
    {"author_id": "A3", "ratio": 30}
  ]
}' | fmt

echo "== 11. 查询当前及历次分成版本；作品详情展示当前分成 =="
curl -s "$BASE/works/BOOK-1/splits" | fmt
curl -s "$BASE/works/BOOK-1/splits/1" | fmt
curl -s "$BASE/works/BOOK-1" | fmt

echo "== 12. 新销售按入账时的分成版本（v2：A1 70 / A3 30）分账 =="
curl -s -X POST "$BASE/sales" -H 'Content-Type: application/json' -d '{
  "sale_id": "S102",
  "work_id": "BOOK-1",
  "business_time": "2026-10-01T09:00:00",
  "copies": 10,
  "net_price": 20.00
}' | fmt

echo "== 13. 旧销售 S100 的退款仍从末册冲回，并按原销售版本（v1：A1 60 / A2 40）分账 =="
curl -s -X POST "$BASE/refunds" -H 'Content-Type: application/json' -d '{
  "refund_id": "RF101",
  "sale_id": "S100",
  "copies": 10
}' | fmt

echo "== 14. 再次关账：退出作者 A2 仍可查询并结算既有余额，新作者 A3 参与关账与结算 =="
curl -s -X POST "$BASE/closes" -H 'Content-Type: application/json' -d '{
  "request_id": "CLOSE-2026-10"
}' | fmt
curl -s "$BASE/authors/A2/settlement" | fmt
curl -s "$BASE/authors/A3/settlement" | fmt

echo "== 15. 销售净价录错：凭全局唯一更正编号对 S100 提交更正（33.33 → 40）=="
echo "    仅对各档未退册数、按原销售版本 v1 计算新旧每册版税差额（独立流水，不改历史）"
curl -s -X POST "$BASE/price-corrections" -H 'Content-Type: application/json' -d '{
  "correction_id": "COR-2026-10-001",
  "sale_id": "S100",
  "new_net_price": 40.00
}' | fmt

echo "== 16. 同更正编号同内容重试（200，返回原结果）；异内容/同价会被拒绝 =="
curl -s -o /dev/null -w 'replay status: %{http_code}\n' -X POST "$BASE/price-corrections" \
  -H 'Content-Type: application/json' -d '{
    "correction_id": "COR-2026-10-001", "sale_id": "S100", "new_net_price": 40.00
  }'
curl -s -o /dev/null -w 'same-price status: %{http_code} (409)\n' -X POST "$BASE/price-corrections" \
  -H 'Content-Type: application/json' -d '{
    "correction_id": "COR-2026-10-002", "sale_id": "S100", "new_net_price": 40.00
  }'

echo "== 17. 按更正编号查询 / 按原销售列出全部更正 =="
curl -s "$BASE/price-corrections/COR-2026-10-001" | fmt
curl -s "$BASE/price-corrections?sale_id=S100" | fmt

echo "== 18. 更正后退款：原价/原版本冲回字段不变，另附已生效价差反向流水 =="
curl -s -X POST "$BASE/refunds" -H 'Content-Type: application/json' -d '{
  "refund_id": "RF102",
  "sale_id": "S100",
  "copies": 100
}' | fmt

echo "== 19. 再次关账：更正与反向作为独立流水进入本批，历史批次与付款不改写 =="
curl -s -X POST "$BASE/closes" -H 'Content-Type: application/json' -d '{
  "request_id": "CLOSE-2026-11"
}' | fmt

echo "== 20. 渠道对账：批次号 + 入账序号区间 (from, to] + 渠道逐笔回单 =="
echo "    区间内仅原销售/退款参与；退款 RF102 净价须按原销售入账价 33.33（更正价 40 不参与）"
curl -s -X POST "$BASE/reconciliations" -H 'Content-Type: application/json' -d '{
  "batch_no": "RECON-CHX-2026-11",
  "channel": "CH-X",
  "from_entry_seq": 0,
  "to_entry_seq": 8,
  "receipts": [
    {"receipt_no": "RCP-001", "type": "sale",   "ref_id": "S100", "copies": 1200, "net_price": 33.33},
    {"receipt_no": "RCP-002", "type": "refund", "ref_id": "RF100", "copies": 250,  "net_price": 33.33},
    {"receipt_no": "RCP-003", "type": "sale",   "ref_id": "S999", "copies": 1,    "net_price": 10.00}
  ]
}' | fmt

echo "== 21. 同批次号同内容重试（回单顺序无关）→ 200 返回同一冻结报告；异内容 → 409 =="
curl -s -o /dev/null -w 'replay status: %{http_code}\n' -X POST "$BASE/reconciliations" \
  -H 'Content-Type: application/json' -d '{
    "batch_no": "RECON-CHX-2026-11", "channel": "CH-X",
    "from_entry_seq": 0, "to_entry_seq": 8,
    "receipts": [
      {"receipt_no": "RCP-003", "type": "sale",   "ref_id": "S999", "copies": 1,    "net_price": 10.00},
      {"receipt_no": "RCP-002", "type": "refund", "ref_id": "RF100", "copies": 250,  "net_price": 33.33},
      {"receipt_no": "RCP-001", "type": "sale",   "ref_id": "S100", "copies": 1200, "net_price": 33.33}
    ]
  }'

echo "== 22. 空回单批次核查内部漏报（整批回单为空，内部流水全部 internal_missing）=="
curl -s -X POST "$BASE/reconciliations" -H 'Content-Type: application/json' -d '{
  "batch_no": "RECON-CHX-EMPTY", "channel": "CH-X",
  "from_entry_seq": 0, "to_entry_seq": 8, "receipts": []
}' | fmt

echo "== 23. 整批拒绝示例：区间越界 + 回单号重复（400，不落库，detail 汇总原因）=="
curl -s -X POST "$BASE/reconciliations" -H 'Content-Type: application/json' -d '{
  "batch_no": "RECON-BAD", "channel": "CH-X",
  "from_entry_seq": 0, "to_entry_seq": 999,
  "receipts": [
    {"receipt_no": "DUP", "type": "sale", "ref_id": "S100", "copies": 1, "net_price": 10},
    {"receipt_no": "DUP", "type": "sale", "ref_id": "S101", "copies": 1, "net_price": 10}
  ]
}' | fmt

echo "== 24. 查询对账批次列表（可按渠道过滤）/ 按编号 / 按批次号取冻结报告 =="
curl -s "$BASE/reconciliations" | fmt
curl -s "$BASE/reconciliations?channel=CH-X" | fmt
curl -s "$BASE/reconciliations/1" | fmt
curl -s "$BASE/reconciliations/by-batch/RECON-CHX-2026-11" | fmt

echo "== 25. 银行退票：凭全局唯一冲回编号 + 原付款编号整笔冲回 PAY-2026-09-A1 =="
echo "    返回关联付款快照、冲回金额（= 原付款整笔金额）与登记时作者结算快照"
curl -s -X POST "$BASE/payment-reversals" -H 'Content-Type: application/json' -d '{
  "reversal_id": "REV-2026-10-PAY-2026-09-A1",
  "payment_id": "PAY-2026-09-A1"
}' | fmt

echo "== 26. 同冲回编号同内容重试 → 200 原结果；换编号重复冲回同一付款 → 409 =="
curl -s -o /dev/null -w 'replay status: %{http_code}\n' -X POST "$BASE/payment-reversals" \
  -H 'Content-Type: application/json' -d '{
    "reversal_id": "REV-2026-10-PAY-2026-09-A1", "payment_id": "PAY-2026-09-A1"
  }'
curl -s -o /dev/null -w 'duplicate status: %{http_code} (409)\n' -X POST "$BASE/payment-reversals" \
  -H 'Content-Type: application/json' -d '{
    "reversal_id": "REV-2026-10-OTHER", "payment_id": "PAY-2026-09-A1"
  }'

echo "== 27. 结算总览：有效已付 = 付款总额 − 冲回总额，可付恢复，冲回记录可查 =="
curl -s "$BASE/authors/A1/settlement" | fmt

echo "== 28. 按冲回编号查询冲回详情（含关联付款快照与登记时结算快照）=="
curl -s "$BASE/payment-reversals/REV-2026-10-PAY-2026-09-A1" | fmt

echo "== 29. 作品关账贡献查询：全部已冻结批次（分批四类流水数量/册数/金额、批次净版税、冻结流水摘要、范围合计）=="
curl -s "$BASE/works/BOOK-1/close-contributions" | fmt

echo "== 32. 待付额度预约（v1.7.0）：独立作品 BOOK-RSV，1000 册 × 10.00 × 10% = 100000 分，关账 =="
curl -s -X POST "$BASE/works" -H 'Content-Type: application/json' -d '{
  "work_id": "BOOK-RSV",
  "tiers": [{"threshold": 0, "rate": 0.10}],
  "authors": [{"author_id": "RA1", "ratio": 100}]
}' | fmt
curl -s -X POST "$BASE/sales" -H 'Content-Type: application/json' -d '{
  "sale_id": "SR1", "work_id": "BOOK-RSV",
  "business_time": "2026-10-06T10:00:00",
  "copies": 1000, "net_price": 10.00
}' | fmt
curl -s -X POST "$BASE/closes" -H 'Content-Type: application/json' -d '{"request_id": "CLOSE-RSV-1"}' > /dev/null
RSV_CLOSE=$(curl -s "$BASE/authors/RA1/settlement" | python3 -c 'import sys,json; print(json.load(sys.stdin)["latest_close_id"])')
echo "    预约/付款使用当前最新关账批次号：$RSV_CLOSE"

echo "== 33. 实际收款前预约 60000 分（全局唯一预约号；可预约额=应计−有效已付−其他待执行预约）=="
curl -s -X POST "$BASE/payment-reservations" -H 'Content-Type: application/json' -d "{
  \"reservation_id\": \"RSV-2026-10-001\", \"author_id\": \"RA1\",
  \"close_id\": $RSV_CLOSE, \"amount_cents\": 60000
}" | fmt
# → 201：status="pending"；available_cents=100000（创建时快照），
#    current_reserved_cents=60000，author_available_cents=40000（当前可用，已扣本预约）
# 同号同内容重试 → 200 原状态；异内容（含换作者/批次/金额）→ 409
curl -s -o /dev/null -w 'same replay: %{http_code} (200)\n' -X POST "$BASE/payment-reservations" \
  -H 'Content-Type: application/json' -d "{
    \"reservation_id\": \"RSV-2026-10-001\", \"author_id\": \"RA1\",
    \"close_id\": $RSV_CLOSE, \"amount_cents\": 60000
  }"
curl -s -o /dev/null -w 'same id diff: %{http_code} (409)\n' -X POST "$BASE/payment-reservations" \
  -H 'Content-Type: application/json' -d "{
    \"reservation_id\": \"RSV-2026-10-001\", \"author_id\": \"RA1\",
    \"close_id\": $RSV_CLOSE, \"amount_cents\": 60001
  }"

echo "== 34. 直接付款也扣待执行占额：40001 分超额 400；另一笔 10000 预约后取消，占额释放 =="
curl -s -o /dev/null -w 'direct over hold: %{http_code} (400)\n' -X POST "$BASE/payments" \
  -H 'Content-Type: application/json' -d "{
    \"payment_id\": \"PAY-DIRECT-RA1\", \"author_id\": \"RA1\",
    \"close_id\": $RSV_CLOSE, \"amount_cents\": 40001
  }"
curl -s -X POST "$BASE/payment-reservations" -H 'Content-Type: application/json' -d "{
  \"reservation_id\": \"RSV-2026-10-002\", \"author_id\": \"RA1\",
  \"close_id\": $RSV_CLOSE, \"amount_cents\": 10000
}" > /dev/null
curl -s -X POST "$BASE/payment-reservations/RSV-2026-10-002/cancel" | fmt
# → status="cancelled"；current_reserved_cents 回到 60000（仅 RSV-001 占额），可用 40000
curl -s -o /dev/null -w 're-cancel: %{http_code} (200 幂等)\n' -X POST "$BASE/payment-reservations/RSV-2026-10-002/cancel"

echo "== 35. 执行预约：同事务校验批次与余额，登记现有付款并解除占额（付款编号全局唯一）=="
curl -s -X POST "$BASE/payment-reservations/RSV-2026-10-001/execute" \
  -H 'Content-Type: application/json' -d '{"payment_id": "PAY-RSV-001"}' | fmt
# → 201：status="executed"，payment 为登记的付款（与 /payments 同口径）；
#    current_paid_cents=60000、current_reserved_cents=0、author_available_cents=40000
curl -s -o /dev/null -w 'replay execute: %{http_code} (200，返回原付款)\n' \
  -X POST "$BASE/payment-reservations/RSV-2026-10-001/execute" \
  -H 'Content-Type: application/json' -d '{"payment_id": "PAY-RSV-001"}'
curl -s -o /dev/null -w 'switch pay id:  %{http_code} (409)\n' \
  -X POST "$BASE/payment-reservations/RSV-2026-10-001/execute" \
  -H 'Content-Type: application/json' -d '{"payment_id": "PAY-RSV-OTHER"}'
curl -s -o /dev/null -w 'cancel executed: %{http_code} (409，状态迁移不可逆)\n' \
  -X POST "$BASE/payment-reservations/RSV-2026-10-001/cancel"
curl -s "$BASE/payments/PAY-RSV-001" | fmt  # 执行登记的付款可直接查询

echo "== 36. 已执行付款仍可按现有口径退票；退完后余额恢复，可再付款 =="
curl -s -X POST "$BASE/payment-reversals" -H 'Content-Type: application/json' -d '{
  "reversal_id": "REV-RSV-001", "payment_id": "PAY-RSV-001"
}' | fmt
curl -s "$BASE/payment-reservations/RSV-2026-10-001" | fmt  # status 仍为 executed

echo "== 37. 新关账使旧批次待执行预约失效并释放占额（先留一笔待执行预约）=="
curl -s -X POST "$BASE/payment-reservations" -H 'Content-Type: application/json' -d "{
  \"reservation_id\": \"RSV-2026-10-003\", \"author_id\": \"RA1\",
  \"close_id\": $RSV_CLOSE, \"amount_cents\": 10000
}" > /dev/null
curl -s -X POST "$BASE/closes" -H 'Content-Type: application/json' -d '{"request_id": "CLOSE-RSV-2"}' | fmt
# → expired_reservations_count=1；RSV-2026-10-003 变为 expired、expired_at_close_id 为新批次
curl -s -o /dev/null -w 'execute expired: %{http_code} (409)\n' \
  -X POST "$BASE/payment-reservations/RSV-2026-10-003/execute" \
  -H 'Content-Type: application/json' -d '{"payment_id": "PAY-RSV-003"}'
curl -s -o /dev/null -w 'cancel expired:  %{http_code} (409)\n' \
  -X POST "$BASE/payment-reservations/RSV-2026-10-003/cancel"

echo "== 38. 错误码与作者结算总览（reserved_cents / available_cents / reservations[]）=="
curl -s -o /dev/null -w 'no author:     %{http_code} (404)\n' -X POST "$BASE/payment-reservations" \
  -H 'Content-Type: application/json' -d '{"reservation_id":"X","author_id":"NOPE","close_id":1,"amount_cents":1}'
curl -s -o /dev/null -w 'no close:      %{http_code} (404)\n' -X POST "$BASE/payment-reservations" \
  -H 'Content-Type: application/json' -d '{"reservation_id":"X","author_id":"RA1","close_id":999,"amount_cents":1}'
curl -s -o /dev/null -w 'expired close: %{http_code} (409)\n' -X POST "$BASE/payment-reservations" \
  -H 'Content-Type: application/json' -d "{\"reservation_id\":\"X\",\"author_id\":\"RA1\",\"close_id\":$RSV_CLOSE,\"amount_cents\":1}"
curl -s -o /dev/null -w 'over quota:    %{http_code} (400)\n' -X POST "$BASE/payment-reservations" \
  -H 'Content-Type: application/json' -d '{"reservation_id":"X","author_id":"RA1","close_id":1,"amount_cents":99999999}'
curl -s -o /dev/null -w 'bad amount:    %{http_code} (422)\n' -X POST "$BASE/payment-reservations" \
  -H 'Content-Type: application/json' -d '{"reservation_id":"X","author_id":"RA1","close_id":1,"amount_cents":0}'
curl -s "$BASE/authors/RA1/settlement" | fmt
