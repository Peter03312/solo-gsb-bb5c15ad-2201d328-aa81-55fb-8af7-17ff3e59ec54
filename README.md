# 数字出版版税结算服务

基于 **Python / FastAPI + SQLite** 的版税结算服务：作品阶梯费率管理、作者分成版本管理、销售入账计提、作者分账、退款冲销、**销售净价更正（价差补提/冲回）**、关账批次报表、**作品关账贡献查询**、**渠道逐笔回单对账（冻结报告）**、**付款冲回（银行退票）**，支持幂等重试与并发安全。

## 快速启动

### Docker（推荐，一键启动并自动初始化数据库）

```bash
docker compose up --build
```

或直接使用 Dockerfile：

```bash
docker build -t royalties .
docker run -d -p 8000:8000 -v royalties-data:/data royalties
```

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

数据库 schema 在服务启动时自动创建（幂等），无需手工初始化。

### 地址与端口

| 地址 | 说明 |
| --- | --- |
| `http://localhost:8000` | 服务入口 |
| `http://localhost:8000/docs` | Swagger 交互式 API 文档 |
| `http://localhost:8000/health` | 健康检查 |

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | 容器内 `/data/royalties.db`，本地 `./royalties.db` | SQLite 数据库文件路径 |
| `PORT` | `8000` | 服务监听端口（Docker 启动命令使用） |

Compose 默认将宿主机 `8000` 映射到容器 `8000`，数据持久化在命名卷 `royalties-data`。

## 业务规则

- **作品**：录入递增的阶梯销量门槛（首档门槛必须为 0，严格递增）、非负费率（小数，如 `0.08` 表示 8%）、作者分成比例（百分数，合计必须恰好为 100）。
- **作者分成版本**：作品的作者分成可随合作关系变化。创建作品时的分成为**版本 1**；之后按 `作品编号 + 全局唯一变更编号 + 作者编号/比例` 提交**新版本**，版本号按提交顺序（数据库提交顺序）递增。作者编号不得重复、比例非负且合计必须为 100；作品不存在返回 404，数据不合法返回 422。同变更编号同内容重试返回原版本（HTTP 200），异内容拒绝（HTTP 409，变更编号**全局唯一**，跨作品复用也算冲突）。**版本按提交顺序生效：新销售使用入账时的当前版本分账；旧销售的后续退款仍从原销售末册冲回并按原销售版本分账；业务时间不决定版本。** 作品详情展示当前分成；作品创建的同号同内容重试仍返回**创建时结果**（版本 1 的分成）。退出分成的作者仍可查询并结算既有余额，新作者可参与后续关账与结算。
- **销售计提**：销售按**入账序号**（数据库提交顺序）累计册数，与业务时间无关；跨档时分档计提。每册版税 = 每册净价 × 该档费率，**四舍五入到分**；分档金额 = 每册版税 × 该档册数。入账时记录当时生效的分成版本（响应字段 `split_version`）。
- **作者分账**：对整笔计提金额，各作者按分成比例**先向下取整到分**，剩余分按**作者编号顺序**（纯数字编号按数值，其余按字典序）依次补 1 分。
- **退款**：从原销售**最后一册**起按原每册计提冲回，分账按**原销售入账时的分成版本**计算，**不回退累计销量**；同一销售的累计退款册数不得超过原册数（超出返回 400）。
- **净价更正**：销售净价录错后，财务凭**全局唯一更正编号**、原销售编号与非负新每册净价提交更正。系统按**原销售阶梯费率与入账时分成版本**，仅对各档**未退册数**计算新旧净价的每册版税差额（每册版税仍四舍五入到分）；**逐作者差额 = 未退款总额按新价整体分账 − 按旧价整体分账**（两个总额各自独立分账后相减，余分规则不变）。旧价取该销售的"当前更正价"（首次更正即原价），故可多次串联更正。更正以**独立流水**（独立入账序号）进入**下一关账批次**，**不改写**历史销售、退款、已关账报表与付款。同更正编号同内容重试返回原结果（200），异内容拒绝（409，**编号全局唯一**、跨销售复用也算冲突）；销售不存在 404、**已全退 400**、新价等于当前更正价 409。
- **更正后退款**：退款本身仍按原销售**末册、原价、原分成版本**冲回，**原有退款字段及金额不变**；同时对**被退册数**另记一条"**已生效价差反向流水**"（同一事务内独立入账序号，随退款归入**同一关账批次**）。各档反向额 =（当前更正价 − 原价）下该档每册价差 × 本次被退册数；逐作者反向额按"未退总额分账之差"**望远镜式**计算（退款前后两个时点各算一次新旧价分账差额再相减），因此**全退后该销售逐作者累计价差恰为 0**（与更正、退款的交错顺序无关）。无已生效价差的退款不产生反向流水（字段为 `null`）；**旧退款不追溯**。更正与退款、关账并发时，按**提交顺序**（数据库提交顺序）判定未退册数与批次归属。
- **关账**：汇总上次关账之后到本次提交前的全部流水（按入账序号），生成**逐笔明细 + 作者合计**的快照报表，落库后**不可修改**（仅提供只读接口）；关账后补录的旧业务流水自然进入下一批次。
- **付款结算（关账后）**：以作者编号、**当前最新关账批次**编号、付款编号、支付金额（分）提交。应计额 = 截至该批次全部**不可改关账快照**中该作者净额之和；已付额 = 该作者全部已确认付款之和；可付额 = `max(应计额 − 已付额, 0)`。支付金额必须为正且不超过可付额；作者/批次不存在返回 404，批次不是当前最新返回 409。**若该作者存在待执行付款预约，直接付款还须扣除预约占额**（`max(应计 − 有效已付 − 待执行预约占额, 0)`），避免绕过预约超付。
- **付款预约（v1.7.0 新增，实际付款前预占待付额度）**：财务在作者实际收款前，可凭**全局唯一预约号**、作者编号、**当前最新关账批次**号和正整数金额（分）预约待付额度。**可预约额 = 该批次累计应计 − 有效已付 − 同作者待执行（pending）预约占额，负值按零计，超额拒绝（400）**。预约状态机：`pending`（待执行）→ `executed`（已执行，终态）/ `canceled`（主动取消，终态）/ `expired`（新关账时旧批次待执行预约失效，终态），**终态不得反向转换**。
  - **执行**：待执行预约可凭**全局唯一付款编号**执行一次——在**同一事务**中校验批次与余额（排除本笔占额后可用额仍须 ≥ 预约金额）、**登记现有口径付款并解除本笔占额**；重复执行（同预约、同付款编号）返回原付款（200），**换付款编号**执行同一预约、或付款编号已被其他付款/预约占用均拒绝（409）。
  - **取消**：待执行预约可取消并**释放占额**（幂等：重复取消返回原状态 200）；已执行/已取消/已失效不得再取消（409）。
  - **关账失效**：**新关账时旧批次的待执行预约统一失效（expired）并释放占额**（关账报表返回本次失效预约数 `expired_payment_reservations_count`）；**已执行付款及后续退票（付款冲回）仍遵循现有记录与结算口径**，不受预约失效影响；退票恢复的金额仍可被预约或直接付款。
  - **幂等与报错**：同号同内容创建重试返回原预约状态（200，即使已随关账失效也返回原状态），同号异内容拒绝（409）；作者或批次不存在 404、过期批次 409、金额非正 422、不允许的状态迁移 409。响应始终返回**预约状态、关联付款（执行后）与作者当前可用额**（`available_cents`）。
  - **并发**：预约创建/执行/取消与直接付款、关账共用同一进程锁与 `BEGIN IMMEDIATE` 单事务，按提交顺序原子判定，并发预约不会超额、并发执行同一预约至多一笔成功。
- **负余额（待抵扣）**：关账后退款若使应计额低于已付额，差额记为作者的待抵扣负余额，**后续关账的正额先抵扣**，旧报表与旧付款均不改写；负余额未清零时可付额为 0，不能付款。
- **付款冲回（银行退票）**：银行退票后，财务凭**全局唯一冲回编号**与**原付款编号**登记**整笔付款冲回**（不支持部分冲回，冲回金额恒为原付款金额）。响应返回**关联付款（提交时快照）、冲回金额及登记时的作者结算快照**（应计、冲回前后有效已付、可付、待抵扣负余额、最新批次）。**每笔付款至多冲回一次**；同冲回编号同内容重试返回原结果（HTTP 200），异内容或换编号重复冲回均拒绝（HTTP 409）；原付款不存在返回 404。**有效已付额 = 付款总额 − 冲回总额**；可付额与待抵扣负余额按**最新关账快照**重算（被退回的金额恢复为可付，可重新付款）；原付款及其提交时快照、既有关账报表均保留不改写，后续关账也不改变已登记的冲回结果；冲回记录可在作者结算总览中查询。冲回与新付款并发时按**提交顺序**（同一全局锁 + 单事务）原子判定，不会超付。
- **幂等**：作品 / 分成变更 / 销售 / 退款 / **净价更正** / 付款 / **付款预约**以唯一编号为准——同号同内容重试返回原结果（HTTP 200），同号异内容拒绝（HTTP 409）；付款与付款预约的同号同内容重试**即使之后又有关账也返回提交时刻的状态/快照**（预约若已随关账失效，重试返回的状态为 `expired`）。预约执行凭全局唯一付款编号：同预约同付款编号重复执行返回原付款（200），换编号拒绝（409）。关账可携带 `request_id` 作为幂等键。
- **并发**：所有入账、分成变更、关账与结算写操作在进程内串行化并在单事务中提交（`BEGIN IMMEDIATE`），版本生效与批次归属由**数据库提交顺序**决定，并发变更与入账有确定先后，并发结算不会超付。
- **渠道对账（v1.4.0 新增）**：财务以**全局唯一批次号**、**入账序号区间 `(from_entry_seq, to_entry_seq]`** 与**渠道**逐笔回单（批内唯一回单号 `receipt_no`、销售/退款类型 `type`、内部流水编号 `ref_id`、册数 `copies`、每册净价 `net_price`）提交对账。
  - **核对口径**：区间内只以**原销售与原退款**流水为准；**净价更正与价差反向流水不参与**。退款内部侧册数取退款册数、每册净价**一律取原销售入账价**（即使销售后续被更正）。回单类型支持 `sale`/`refund`（兼容中文 `销售`/`退款`）。
  - **逐笔结论 `status`**：`matched`（一致：册数与净价均相符）、`value_mismatch`（册数或净价不符，行内 `copies_match`/`net_price_match` 指出差异项）、`channel_extra`（渠道多报：回单在区间内无同 `(类型, 内部编号)` 流水，`reason` 为 `not_found` 内部无此编号 / `kind_mismatch` 编号存在但类型不符 / `not_in_interval` 同类型流水存在但序号越出本批区间）、`internal_missing`（内部漏报：区间内有内部流水但渠道未提交对应回单）。每行均给出**双方原值**（`channel`/`internal` 的册数、每册净价、金额分），报告顶层给出**双方各自汇总**（销售/退款笔数、册数、金额与净额 `net_amount_cents`）。
  - **空回单可核漏报**：整批回单列表可以为空（此时区间内全部内部流水落入内部漏报）；单笔回单允许 `copies=0` 的空回单（仍按类型与内部编号参与匹配，用于核查漏报；金额为 0）。
  - **整批拒绝（HTTP 400，不落库、不生成报告）**：同一内部流水 `(type, ref_id)` 出现多次、回单号批内重复、区间非正整数长度或终点越过当前最大入账序号（区间越界）；修正后可用同批次号重新提交。请求体数值非法（负数、非有限数、非法类型等）返回 **422**。
  - **快照与冻结**：核对与保存在**同一数据快照**中完成（持全局锁、单事务读取区间快照并落库）；成功后的报告**冻结**，后续补录、退款或更正**不改变旧报告**。
  - **幂等与冲突**：同批次号同内容（回单提交顺序无关）重试返回原冻结报告（HTTP 200）；同批次号异内容（区间/渠道/回单不同）拒绝（HTTP 409）。
  - **只读不改写**：对账只读取销售/退款数据并写入独立的 `reconciliations` 表，**不改写**销售、退款、关账与付款；对账不占用入账序号、不影响关账批次。已有接口字段全部保持兼容。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/works` | 录入作品（阶梯费率 + 初始作者分成，即版本 1） |
| GET | `/works/{work_id}` | 查询作品（展示**当前**分成版本） |
| GET | `/works/{work_id}/stats` | 作品销量汇总（累计/已退/净册数） |
| GET | `/works/{work_id}/close-contributions` | 作品关账贡献查询（分批四类流水数量/册数/金额、批次净版税、冻结流水摘要、范围合计；可选 `?from_close=&to_close=`，起止均含） |
| POST | `/works/{work_id}/splits` | 提交作者分成**新版本**（变更编号幂等，按提交顺序生效） |
| GET | `/works/{work_id}/splits` | 查询当前及历次分成版本 |
| GET | `/works/{work_id}/splits/{version}` | 查询指定分成版本 |
| POST | `/sales` | 登记销售流水，返回分档计提与分账明细（含入账时的 `split_version`） |
| GET | `/sales/{sale_id}` | 查询销售流水 |
| POST | `/refunds` | 登记退款流水，返回冲销明细（按原销售版本分账；若该销售已更正，另附被退册数价差反向流水） |
| GET | `/refunds/{refund_id}` | 查询退款流水 |
| POST | `/price-corrections` | 提交销售净价更正（全局唯一更正编号；仅对各档未退册数补提/冲回差额） |
| GET | `/price-corrections/{correction_id}` | 按更正编号查询更正流水 |
| GET | `/price-corrections` | 查询更正列表（可选 `?sale_id=` 按原销售过滤） |
| POST | `/closes` | 关账，生成不可改报表（可选 `request_id` 幂等键） |
| GET | `/closes` | 关账批次列表 |
| GET | `/closes/{close_id}` | 查询关账报表 |
| POST | `/payments` | 提交作者付款结算（仅当前最新关账批次；扣除待执行预约占额） |
| GET | `/payments/{payment_id}` | 查询单笔付款结算 |
| POST | `/payment-reservations` | 预约待付额度（全局唯一预约号；可预约额=应计−有效已付−待执行预约占额） |
| GET | `/payment-reservations/{reservation_id}` | 查询付款预约（状态、关联付款、作者当前可用额） |
| POST | `/payment-reservations/{reservation_id}/execute` | 执行付款预约（全局唯一付款编号；同事务校验/登记付款/解除占额） |
| POST | `/payment-reservations/{reservation_id}/cancel` | 取消付款预约并释放占额（仅待执行可取消，幂等） |
| POST | `/payment-reversals` | 登记付款冲回（银行退票；全局唯一冲回编号，整笔冲回） |
| GET | `/payment-reversals/{reversal_id}` | 按冲回编号查询付款冲回 |
| GET | `/authors/{author_id}/settlement` | 作者结算总览：分批应计、已付、负余额、可付、付款与冲回记录 |
| POST | `/reconciliations` | 提交渠道对账批次（批次号幂等；报告核对后冻结落库） |
| GET | `/reconciliations` | 对账批次列表（可选 `?channel=` 按渠道过滤） |
| GET | `/reconciliations/{reconciliation_id}` | 按自增编号查询冻结对账报告 |
| GET | `/reconciliations/by-batch/{batch_no}` | 按批次号查询冻结对账报告 |

金额单位：响应中 `*_cents` 字段均为**分**；`net_price` 为元（字符串原样返回，避免浮点误差）。

## 调用示例

完整可运行脚本：`./examples/demo.sh`（需服务已启动）。

```bash
# 1. 录入作品：两档阶梯，两位作者 60/40 分成
curl -X POST http://localhost:8000/works -H 'Content-Type: application/json' -d '{
  "work_id": "BOOK-1",
  "title": "示例作品",
  "tiers": [{"threshold": 0, "rate": 0.08}, {"threshold": 1000, "rate": 0.10}],
  "authors": [{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 40}]
}'

# 2. 登记销售 1200 册（跨档计提：1000 册×8% + 200 册×10%）
curl -X POST http://localhost:8000/sales -H 'Content-Type: application/json' -d '{
  "sale_id": "S100", "work_id": "BOOK-1",
  "business_time": "2026-09-01T10:00:00",
  "copies": 1200, "net_price": 33.33
}'
# → total_royalty_cents = 333600（1000×267 + 200×333）
# → splits: A1 200160 分，A2 133440 分

# 3. 退款 250 册（从最后一册起冲回：200 册×333 + 50 册×267 = 79950 分）
curl -X POST http://localhost:8000/refunds -H 'Content-Type: application/json' -d '{
  "refund_id": "RF100", "sale_id": "S100", "copies": 250
}'

# 4. 关账，生成不可改报表（request_id 为幂等键，重试返回原报表）
curl -X POST http://localhost:8000/closes -H 'Content-Type: application/json' -d '{
  "request_id": "CLOSE-2026-09"
}'

# 5. 查看报表与批次列表
curl http://localhost:8000/closes/1
curl http://localhost:8000/closes

# 6. 财务确认作者实际付款（close_id 必须是当前最新批次）
curl -X POST http://localhost:8000/payments -H 'Content-Type: application/json' -d '{
  "payment_id": "PAY-2026-09-A1",
  "author_id": "A1",
  "close_id": 2,
  "amount_cents": 100000
}'

# 7. 同付款编号同内容重试 → 原样返回；按作者查询结算总览
curl -X POST http://localhost:8000/payments -H 'Content-Type: application/json' -d '{
  "payment_id": "PAY-2026-09-A1", "author_id": "A1", "close_id": 2, "amount_cents": 100000
}'
curl http://localhost:8000/authors/A1/settlement
curl http://localhost:8000/payments/PAY-2026-09-A1

# 8. 合作关系变化：提交作者分成新版本（A2 退出、A3 加入；变更编号全局唯一）
curl -X POST http://localhost:8000/works/BOOK-1/splits -H 'Content-Type: application/json' -d '{
  "change_id": "CHG-2026-10-01",
  "authors": [{"author_id": "A1", "ratio": 70}, {"author_id": "A3", "ratio": 30}]
}'
# → 201 {"work_id": "BOOK-1", "version": 2, "change_id": "CHG-2026-10-01", "authors": [...], ...}
#    同号同内容重试 → 200 返回原版本；同号异内容（含跨作品复用编号）→ 409

# 9. 查询当前及历次分成版本；作品详情展示当前分成
curl http://localhost:8000/works/BOOK-1/splits        # current_version=2，含版本 1/2 全量
curl http://localhost:8000/works/BOOK-1/splits/1      # 指定历史版本
curl http://localhost:8000/works/BOOK-1               # authors 为当前版本（A1 70 / A3 30）

# 10. 新销售按入账时的分成版本（v2）分账
curl -X POST http://localhost:8000/sales -H 'Content-Type: application/json' -d '{
  "sale_id": "S102", "work_id": "BOOK-1",
  "business_time": "2026-10-01T09:00:00",
  "copies": 10, "net_price": 20.00
}'
# → split_version=2，splits 为 A1 / A3

# 11. 旧销售 S100 的退款仍从末册冲回，并按原销售版本（v1）分账
curl -X POST http://localhost:8000/refunds -H 'Content-Type: application/json' -d '{
  "refund_id": "RF101", "sale_id": "S100", "copies": 10
}'
# → split_version=1，splits 为 A1 / A2（60/40）；price_delta_reversal 为 null（未更正过，旧退款不追溯）

# 12. 再次关账后：退出作者 A2 仍可查询并结算既有余额，新作者 A3 参与关账与结算
curl -X POST http://localhost:8000/closes -H 'Content-Type: application/json' -d '{"request_id": "CLOSE-2026-10"}'
curl http://localhost:8000/authors/A2/settlement
curl http://localhost:8000/authors/A3/settlement
```

### 净价更正调用示例

```bash
# 13. 销售净价录错：凭全局唯一更正编号 + 原销售编号 + 非负新每册净价更正
#     仅对各档未退册数，按原销售阶梯费率与入账时分成版本计算新旧每册版税差额
curl -X POST http://localhost:8000/price-corrections -H 'Content-Type: application/json' -d '{
  "correction_id": "COR-2026-10-001",
  "sale_id": "S100",
  "new_net_price": 40.00
}'
# → 201：各档 old/new 每册版税、未退册数与差额，逐作者差额，total_delta_cents，
#        sale_entry_seq（原销售入账序号）、entry_seq（更正独立流水序号）、split_version

# 14. 同更正编号同内容重试 → 200 返回原结果；异内容 → 409
curl -X POST http://localhost:8000/price-corrections -H 'Content-Type: application/json' -d '{
  "correction_id": "COR-2026-10-001", "sale_id": "S100", "new_net_price": 40.00
}'

# 15. 按更正编号查询；或按原销售列出全部更正（按入账序号）
curl http://localhost:8000/price-corrections/COR-2026-10-001
curl "http://localhost:8000/price-corrections?sale_id=S100"

# 16. 更正后再退款：原退款字段/金额不变（末册、原价、原版本），
#     响应另附 price_delta_reversal——被退册数已生效价差的反向流水（独立入账序号，同批次）
curl -X POST http://localhost:8000/refunds -H 'Content-Type: application/json' -d '{
  "refund_id": "RF102", "sale_id": "S100", "copies": 100
}'
# → total_refund_cents 仍按原价；price_delta_reversal.splits 为带符号反向分账。
#    全退后该销售逐作者"更正 − 反向"累计价差恰为 0。

# 17. 再次关账：更正与反向作为独立流水进入本批，历史批次与历史付款均不改写
curl -X POST http://localhost:8000/closes -H 'Content-Type: application/json' -d '{"request_id": "CLOSE-2026-11"}'
```

净价更正响应示例（`POST /price-corrections`；S100 已退 260 册，剩余 940 册在 8% 档）：

```json
{
  "type": "price_correction",
  "correction_id": "COR-2026-10-001",
  "sale_id": "S100",
  "work_id": "BOOK-1",
  "sale_entry_seq": 1,
  "entry_seq": 8,
  "old_net_price": "33.33",
  "new_net_price": "40",
  "split_version": 1,
  "segments": [
    {"segment_index": 0, "sale_segment_index": 0, "tier_index": 0, "rate": "0.08",
     "unrefunded_copies": 940,
     "old_per_copy_royalty_cents": 267, "new_per_copy_royalty_cents": 320,
     "delta_per_copy_cents": 53, "amount_cents": 49820}
  ],
  "splits": [
    {"author_id": "A1", "amount_cents": 29892},
    {"author_id": "A2", "amount_cents": 19928}
  ],
  "total_delta_cents": 49820,
  "close_id": null,
  "created_at": "2026-10-05T09:00:00+00:00"
}
```

退款响应中的价差反向流水（`POST /refunds` 的 `price_delta_reversal` 字段；未更正或旧退款为 `null`；下例为 S100 更正后再退 100 册，均落在剩余的 8% 档）：

```json
{
  "refund_id": "RF102",
  "sale_id": "S100",
  "total_refund_cents": 26700,
  "splits": [{"author_id": "A1", "amount_cents": 16020}, {"author_id": "A2", "amount_cents": 10680}],
  "price_delta_reversal": {
    "type": "price_delta_reversal",
    "refund_id": "RF102",
    "sale_id": "S100",
    "entry_seq": 8,
    "split_version": 1,
    "segments": [
      {"segment_index": 0, "sale_segment_index": 0, "tier_index": 0, "rate": "0.08",
       "copies": 100,
       "current_per_copy_royalty_cents": 320, "original_per_copy_royalty_cents": 267,
       "delta_per_copy_cents": 53, "amount_cents": 5300}
    ],
    "splits": [
      {"author_id": "A1", "amount_cents": 3180},
      {"author_id": "A2", "amount_cents": 2120}
    ],
    "total_reversal_cents": 5300
  }
}
```

关账报表新增（既有字段与口径不变）：`price_corrections_count`、`price_delta_reversals_count`、
`total_price_correction_cents`（带符号：补提为正/冲回为负）、`total_price_delta_reversal_cents`（带符号）、
`total_author_net_cents`（含更正与反向的作者净分账合计）。`lines` 中新增
`"type": "price_correction"` 与 `"type": "price_delta_reversal"` 两类逐笔明细。

### 渠道对账调用示例（v1.4.0）

```bash
# 18. 财务提交渠道对账：全局唯一批次号 + 入账序号区间 (from, to] + 渠道逐笔回单
#     区间内仅核对原销售/退款；退款净价必须按原销售入账价（33.33，而非更正后的 40）
curl -X POST http://localhost:8000/reconciliations -H 'Content-Type: application/json' -d '{
  "batch_no": "RECON-CHX-2026-11",
  "channel": "CH-X",
  "from_entry_seq": 0,
  "to_entry_seq": 8,
  "receipts": [
    {"receipt_no": "RCP-001", "type": "sale",   "ref_id": "S100", "copies": 1200, "net_price": 33.33},
    {"receipt_no": "RCP-002", "type": "refund", "ref_id": "RF100", "copies": 250, "net_price": 33.33},
    {"receipt_no": "RCP-003", "type": "sale",   "ref_id": "S999", "copies": 1,   "net_price": 10.00}
  ]
}'
# → 201：S100 matched；RF100 matched（内部取原销售价 33.33）；
#        S999 channel_extra(渠道多报，reason=not_found)；区间内其余未回单流水 internal_missing(内部漏报)。

# 19. 空回单列表亦可提交（核查区间内全部内部漏报）；单笔空回单 copies=0 同样合法
curl -X POST http://localhost:8000/reconciliations -H 'Content-Type: application/json' -d '{
  "batch_no": "RECON-CHX-EMPTY", "channel": "CH-X",
  "from_entry_seq": 0, "to_entry_seq": 8, "receipts": []
}'

# 20. 同批次号同内容重试（回单顺序无关）→ 200 返回同一冻结报告；异内容 → 409
curl -X POST http://localhost:8000/reconciliations -H 'Content-Type: application/json' -d '{
  "batch_no": "RECON-CHX-2026-11", "channel": "CH-X",
  "from_entry_seq": 0, "to_entry_seq": 8,
  "receipts": [
    {"receipt_no": "RCP-003", "type": "sale",   "ref_id": "S999", "copies": 1,    "net_price": 10.00},
    {"receipt_no": "RCP-002", "type": "refund", "ref_id": "RF100", "copies": 250,  "net_price": 33.33},
    {"receipt_no": "RCP-001", "type": "sale",   "ref_id": "S100", "copies": 1200, "net_price": 33.33}
  ]
}'

# 21. 报告冻结：成功后的补录/退款/更正不改旧报告；查询按自增编号或批次号
curl http://localhost:8000/reconciliations
curl "http://localhost:8000/reconciliations?channel=CH-X"
curl http://localhost:8000/reconciliations/1
curl http://localhost:8000/reconciliations/by-batch/RECON-CHX-2026-11
```

整批拒绝示例（HTTP 400，`detail` 给出全部原因；不落库、不分配编号，修正后可用同批次号重提）：

```bash
# 回单号重复 / 同一 (type, ref_id) 多次 / 区间越界（to_entry_seq 超过当前最大入账序号）
curl -X POST http://localhost:8000/reconciliations -H 'Content-Type: application/json' -d '{
  "batch_no": "RECON-BAD", "channel": "CH-X",
  "from_entry_seq": 0, "to_entry_seq": 999,
  "receipts": [
    {"receipt_no": "DUP", "type": "sale", "ref_id": "S100", "copies": 1, "net_price": 10},
    {"receipt_no": "DUP", "type": "sale", "ref_id": "S101", "copies": 1, "net_price": 10}
  ]
}'
# → 400 {"detail": "整批拒绝：回单号批内重复：DUP；区间终点 999 超出当前最大入账序号 8（区间越界）"}
```

对账报告响应示例：

```json
{
  "reconciliation_id": 1,
  "batch_no": "RECON-CHX-2026-11",
  "channel": "CH-X",
  "from_entry_seq": 0,
  "to_entry_seq": 8,
  "entry_seq_from": 1,
  "entry_seq_to": 8,
  "snapshot_max_entry_seq": 8,
  "created_at": "2026-10-05T12:00:00+00:00",
  "counts": {
    "receipts": 3, "internal": 6,
    "matched": 2, "value_mismatch": 0, "channel_extra": 1, "internal_missing": 4
  },
  "lines": [
    {
      "seq": 1, "status": "matched", "type": "sale", "type_label": "销售",
      "receipt_no": "RCP-001", "ref_id": "S100", "entry_seq": 1,
      "copies_match": true, "net_price_match": true, "reason": null,
      "channel":  {"receipt_no": "RCP-001", "copies": 1200, "net_price": "33.33", "amount_cents": 3999600},
      "internal": {"entry_seq": 1, "copies": 1200, "net_price": "33.33", "amount_cents": 3999600}
    },
    {
      "seq": 3, "status": "matched", "type": "refund", "type_label": "退款",
      "receipt_no": "RCP-002", "ref_id": "RF100", "entry_seq": 2,
      "copies_match": true, "net_price_match": true, "reason": null,
      "channel":  {"receipt_no": "RCP-002", "copies": 250, "net_price": "33.33", "amount_cents": 833250},
      "internal": {"entry_seq": 2, "copies": 250, "net_price": "33.33", "amount_cents": 833250}
    },
    {
      "seq": 6, "status": "internal_missing", "type": "refund", "type_label": "退款",
      "receipt_no": null, "ref_id": "RF102", "entry_seq": 7,
      "copies_match": null, "net_price_match": null, "reason": null,
      "channel": null,
      "internal": {"entry_seq": 7, "copies": 100, "net_price": "33.33", "amount_cents": 333300}
    },
    {
      "seq": 7, "status": "channel_extra", "type": "sale", "type_label": "销售",
      "receipt_no": "RCP-003", "ref_id": "S999", "entry_seq": null,
      "copies_match": null, "net_price_match": null, "reason": "not_found",
      "channel":  {"receipt_no": "RCP-003", "copies": 1, "net_price": "10", "amount_cents": 1000},
      "internal": null
    }
  ],
  "channel_summary": {
    "sales": {"count": 2, "copies": 1201, "amount_cents": 4000600},
    "refunds": {"count": 1, "copies": 250, "amount_cents": 833250},
    "net_amount_cents": 3167350
  },
  "internal_summary": {
    "sales": {"count": 3, "copies": 1215, "amount_cents": 4029600},
    "refunds": {"count": 3, "copies": 360, "amount_cents": 1199880},
    "net_amount_cents": 2829720
  }
}
```

字段说明：

- `counts`：`receipts` 回单笔数（含空回单）、`internal` 区间内原销售+原退款笔数，及四类结论计数。
- `lines[].status`：`matched` / `value_mismatch` / `channel_extra` / `internal_missing`；`type_label` 为中文类型。行序：内部基准行按 `entry_seq` 升序，`channel_extra` 行按回单提交顺序追加。
- `lines[].channel` / `lines[].internal`：双方原值（册数、每册净价、金额分；内部侧附 `entry_seq`，渠道侧附 `receipt_no`）；无对侧时为 `null`。
- `lines[].copies_match` / `net_price_match`：仅 `matched`/`value_mismatch` 行有布尔值，其余为 `null`。
- `lines[].reason`（仅 `channel_extra`）：`not_found`（内部无此销售/退款编号）、`kind_mismatch`（编号存在但销售/退款类型不符）、`not_in_interval`（同类型流水存在但入账序号越出本批区间）。
- 金额（分）= 每册净价四舍五入到分 × 册数；`net_amount_cents = 销售金额 − 退款金额`，渠道侧与内部侧各自独立汇总。
- 区间内的净价更正与价差反向流水**不出现在任何行中**；退款内部侧净价恒为原销售入账价。

### 错误码（渠道对账）

| 状态码 | 场景 |
| --- | --- |
| 400 | 整批拒绝：回单号批内重复 / 同一 `(type, ref_id)` 多次出现 / 区间非正长度 / 区间终点越界（`detail` 汇总全部原因，不落库） |
| 404 | 按编号或批次号查询的对账报告不存在 |
| 409 | 同批次号异内容重试（区间、渠道或回单内容不一致） |
| 422 | 请求体校验失败（负数册数/净价、非法类型、编号为空、起点为负等） |


### 错误码（净价更正）

| 状态码 | 场景 |
| --- | --- |
| 400 | 原销售已全部退款（无未退册数可更正） |
| 404 | 原销售不存在（列表按不存在的 `sale_id` 过滤亦为 404） |
| 409 | 同更正编号异内容（含跨销售复用编号）；或新价等于当前更正价 |
| 422 | 请求体校验失败（如 `new_net_price < 0`） |

付款结算响应示例：

```json
{
  "payment_id": "PAY-2026-09-A1",
  "author_id": "A1",
  "close_id": 2,
  "amount_cents": 100000,
  "accrued_cents": 152790,
  "paid_before_cents": 0,
  "payable_cents": 152790,
  "paid_after_cents": 100000,
  "negative_balance_cents": 0,
  "created_at": "2026-10-04T12:00:00+00:00"
}
```

作者结算总览响应（`GET /authors/{author_id}/settlement`）：

```json
{
  "author_id": "A1",
  "latest_close_id": 2,
  "accrued_cents": 152790,
  "paid_cents": 100000,
  "reversed_cents": 0,
  "negative_balance_cents": 0,
  "payable_cents": 52790,
  "batches": [
    {"close_id": 1, "batch_net_cents": 152190, "accrued_cents": 152190, "created_at": "..."},
    {"close_id": 2, "batch_net_cents": 600,    "accrued_cents": 152790, "created_at": "..."}
  ],
  "payments": [
    {"payment_id": "PAY-2026-09-A1", "close_id": 2, "amount_cents": 100000,
     "accrued_cents": 152790, "paid_before_cents": 0, "payable_cents": 152790, "created_at": "..."}
  ],
  "reversals": []
}
```

字段说明：`batches[].batch_net_cents` 为该批次快照中作者净额（退款可为负），`accrued_cents` 为截至该批次的累计应计；顶层 `paid_cents` 为**有效已付额**（付款总额 − 冲回总额），`reversed_cents` 为已冲回总额，`reserved_cents` 为**待执行付款预约占额合计**，`negative_balance_cents = max(paid − accrued, 0)`，`payable_cents = max(accrued − paid, 0)`，`available_cents = max(accrued − paid − reserved, 0)`（作者当前可用额，预约与直接付款均按此额度判定）；`reversals` 为该作者的付款冲回记录（含登记时结算快照），`reservations` 为该作者全部状态的付款预约记录。

### 错误码（付款结算）

| 状态码 | 场景 |
| --- | --- |
| 400 | 支付金额超过可付额（含可付额为 0 / 存在待抵扣负余额 / 待执行预约占额后余额不足） |
| 404 | 作者或关账批次不存在 |
| 409 | 批次不是当前最新；或同付款编号异内容重试 |
| 422 | 请求体校验失败（如 `amount_cents ≤ 0`） |

### 付款冲回调用示例（v1.5.0）

```bash
# 22. 银行退票：凭全局唯一冲回编号 + 原付款编号登记整笔冲回（不支持部分冲回）
curl -X POST http://localhost:8000/payment-reversals -H 'Content-Type: application/json' -d '{
  "reversal_id": "REV-2026-10-PAY-2026-09-A1",
  "payment_id": "PAY-2026-09-A1"
}'
# → 201：关联付款快照、冲回金额（= 原付款整笔金额）、登记时作者结算快照

# 23. 同冲回编号同内容重试 → 200 返回原结果（即使之后又有关账也不变）；
#     同号异内容 / 换编号重复冲回同一付款 → 409
curl -X POST http://localhost:8000/payment-reversals -H 'Content-Type: application/json' -d '{
  "reversal_id": "REV-2026-10-PAY-2026-09-A1", "payment_id": "PAY-2026-09-A1"
}'

# 24. 结算总览：有效已付 = 付款总额 − 冲回总额，可付额恢复，冲回记录可查；
#     被退回的金额可按最新批次重新付款
curl http://localhost:8000/authors/A1/settlement
curl http://localhost:8000/payment-reversals/REV-2026-10-PAY-2026-09-A1
```

付款冲回响应示例（`POST /payment-reversals`；紧接上例 PAY-2026-09-A1 付款 100000 分）：

```json
{
  "reversal_id": "REV-2026-10-PAY-2026-09-A1",
  "payment_id": "PAY-2026-09-A1",
  "author_id": "A1",
  "amount_cents": 100000,
  "payment": {
    "payment_id": "PAY-2026-09-A1",
    "author_id": "A1",
    "close_id": 2,
    "amount_cents": 100000,
    "accrued_cents": 152790,
    "paid_before_cents": 0,
    "payable_cents": 152790,
    "paid_after_cents": 100000,
    "negative_balance_cents": 0,
    "created_at": "2026-10-04T12:00:00+00:00"
  },
  "accrued_cents": 152790,
  "paid_before_cents": 100000,
  "paid_after_cents": 0,
  "payable_cents": 152790,
  "negative_balance_cents": 0,
  "latest_close_id": 2,
  "created_at": "2026-10-05T08:30:00+00:00"
}
```

字段说明：`payment` 为**关联付款的提交时快照**（不随冲回与后续关账变化）；`amount_cents` 为冲回金额（恒等于原付款整笔金额）；`accrued_cents` / `paid_before_cents` / `paid_after_cents` / `payable_cents` / `negative_balance_cents` / `latest_close_id` 为**登记时的作者结算快照**（按当时最新关账批次计算，落库后不再变化）。作者结算总览新增 `reversed_cents`（已冲回总额）与 `reversals`（冲回记录列表）；`paid_cents` 为有效已付额（付款总额 − 冲回总额）。

### 错误码（付款冲回）

| 状态码 | 场景 |
| --- | --- |
| 404 | 原付款不存在；或按冲回编号查询的冲回不存在 |
| 409 | 同冲回编号异内容；或换编号重复冲回同一付款（每笔付款至多冲回一次） |
| 422 | 请求体校验失败（冲回编号 / 原付款编号为空或缺失） |

### 付款预约调用示例（v1.7.0）

```bash
# 25. 作者实际收款前预约待付额度：全局唯一预约号 + 作者 + 当前最新关账批次 + 正整数金额（分）
#     可预约额 = 该批次累计应计 − 有效已付 − 同作者待执行预约占额（负值按零计）
curl -X POST http://localhost:8000/payment-reservations -H 'Content-Type: application/json' -d '{
  "reservation_id": "RSV-2026-10-A1-001",
  "author_id": "A1",
  "close_id": 2,
  "amount_cents": 50000
}'
# → 201：status=pending；accrued/paid/reserved/payable 为创建时快照；
#        available_cents 为作者当前可用额（扣除含本笔在内的待执行预约占额）

# 26. 同号同内容重试 → 200 返回原预约状态（即使之后已关账失效也返回原状态）；同号异内容 → 409
curl -X POST http://localhost:8000/payment-reservations -H 'Content-Type: application/json' -d '{
  "reservation_id": "RSV-2026-10-A1-001", "author_id": "A1", "close_id": 2, "amount_cents": 50000
}'

# 27. 直接付款同样被占额拦截，不能绕过预约
#     （可付额按 max(应计 − 有效已付 − 待执行预约占额, 0) 校验，超额 → 400）
curl -X POST http://localhost:8000/payments -H 'Content-Type: application/json' -d '{
  "payment_id": "PAY-BYPASS", "author_id": "A1", "close_id": 2, "amount_cents": 99999999
}'

# 28. 执行预约：同一事务内校验批次与余额、登记付款并解除占额；付款编号全局唯一
curl -X POST http://localhost:8000/payment-reservations/RSV-2026-10-A1-001/execute \
  -H 'Content-Type: application/json' -d '{"payment_id": "PAY-2026-10-A1-001"}'
# → 201：status=executed，payment 为登记的付款（与 GET /payments/{id} 一致），占额解除
# 重复执行（同付款编号）→ 200 返回原付款；换付款编号 → 409
curl -X POST http://localhost:8000/payment-reservations/RSV-2026-10-A1-001/execute \
  -H 'Content-Type: application/json' -d '{"payment_id": "PAY-2026-10-A1-001"}'

# 29. 取消待执行预约（释放占额，幂等）；已执行/已取消/已失效不得转换 → 409
curl -X POST http://localhost:8000/payment-reservations/RSV-2026-10-A1-002/cancel \
  -H 'Content-Type: application/json' -d '{"reason": "付款计划变更"}'

# 30. 新关账：旧批次 pending 预约自动失效（expired）并释放占额；
#     报表返回 expired_payment_reservations_count；已执行付款及其退票仍按现有口径
curl -X POST http://localhost:8000/closes -H 'Content-Type: application/json' -d '{}'
#     失效后：执行/取消均 409；可在新批次用新预约号重新预约
curl http://localhost:8000/payment-reservations/RSV-2026-10-A1-001
curl http://localhost:8000/authors/A1/settlement
# → 总览新增 reserved_cents（待执行占额）、available_cents（当前可用额）与 reservations 列表
```

付款预约响应示例（`POST /payment-reservations`，创建时；作者累计应计 152790 分）：

```json
{
  "reservation_id": "RSV-2026-10-A1-001",
  "author_id": "A1",
  "close_id": 2,
  "amount_cents": 50000,
  "status": "pending",
  "payment_id": null,
  "payment": null,
  "accrued_cents": 152790,
  "paid_cents": 0,
  "reserved_cents": 0,
  "payable_cents": 102790,
  "latest_close_id": 2,
  "current_accrued_cents": 152790,
  "current_paid_cents": 0,
  "current_reserved_cents": 50000,
  "available_cents": 102790,
  "cancel_reason": null,
  "expired_close_id": null,
  "created_at": "2026-10-06T08:00:00+00:00",
  "executed_at": null,
  "canceled_at": null,
  "expired_at": null
}
```

字段说明：`status` 为预约状态（`pending` / `executed` / `canceled` / `expired`，终态不可反向转换）；`payment_id` / `payment` 在执行后关联登记的付款（其余状态为 `null`）；`accrued_cents` / `paid_cents` / `reserved_cents` / `payable_cents` 为**创建时刻快照**（`reserved_cents` 不含本笔，`payable_cents` 为扣除本笔后的可付额，幂等重放原样返回）；`latest_close_id` / `current_accrued_cents` / `current_paid_cents` / `current_reserved_cents` / `available_cents` 为**作者当前额度**（按当前最新关账批次实时重算，`available_cents = max(应计 − 有效已付 − 待执行预约占额, 0)`）；`expired_close_id` 指出预约因哪一次新关账失效。作者结算总览新增 `reserved_cents`、`available_cents` 与 `reservations`（全部状态的预约记录）；既有 `payable_cents` 等字段保持原口径不变。

### 错误码（付款预约）

| 状态码 | 场景 |
| --- | --- |
| 400 | 预约金额超过可预约额；或执行时余额已不足（应计−有效已付−其他预约占额 < 预约金额） |
| 404 | 作者、关账批次或预约不存在 |
| 409 | 批次不是当前最新（过期批次）；同预约号异内容；换付款编号重复执行；付款编号已被其他付款/预约占用；对已执行/已取消/已失效预约执行或取消等非法状态迁移 |
| 422 | 请求体校验失败（预约号/作者/付款编号为空、`close_id` 或 `amount_cents` 非正整数等） |

响应示例（销售入账，节选）：

```json
{
  "sale_id": "S100",
  "entry_seq": 1,
  "cum_before": 0,
  "cum_after": 1200,
  "split_version": 1,
  "segments": [
    {"tier_index": 0, "rate": "0.08", "copies": 1000, "per_copy_royalty_cents": 267, "amount_cents": 267000},
    {"tier_index": 1, "rate": "0.1",  "copies": 200,  "per_copy_royalty_cents": 333, "amount_cents": 66600}
  ],
  "total_royalty_cents": 333600,
  "splits": [
    {"author_id": "A1", "amount_cents": 200160},
    {"author_id": "A2", "amount_cents": 133440}
  ],
  "close_id": null
}
```

分成版本响应示例（`POST /works/{work_id}/splits`、`GET /works/{work_id}/splits/{version}`）：

```json
{
  "work_id": "BOOK-1",
  "version": 2,
  "change_id": "CHG-2026-10-01",
  "authors": [
    {"author_id": "A1", "ratio": "70"},
    {"author_id": "A3", "ratio": "30"}
  ],
  "created_at": "2026-10-05T08:00:00+00:00"
}
```

### 错误码（分成版本变更）

| 状态码 | 场景 |
| --- | --- |
| 404 | 作品不存在 |
| 409 | 同变更编号异内容（含变更编号跨作品复用） |
| 422 | 请求体校验失败（作者编号重复 / 比例为负 / 合计不为 100 / 空作者列表） |

### 作品关账贡献查询（v1.6.0 新增）

调用方提交**作品编号**与**可选关账批次范围**（`from_close` / `to_close`，**起止均含**），返回每个已冻结关账批次中该作品的：

- **销售、退款、净价更正、价差反向流水**各自的**流水数量、册数与金额（分）**；
- **批次净版税** = 销售 − 退款 + 净价更正 − 价差反向（与关账报表 `total_author_net_cents` 同口径的该作品部分）；
- 按**入账顺序**（`entry_seq` 升序）排列的**冻结流水摘要**（类型、编号、册数、流水金额、带符号净贡献等）；
- 顶层 **`totals` 范围合计**（各字段跨批求和、`batch_count` 为命中批次数）。

口径与边界：

- **仅已存在作品可查**：作品不存在返回 **404**；
- **未提供范围**：查询**全部已冻结批次**；仅提供一端时另一端分别取 `1` / 当前最新批次；
- 起止必须为正整数且起点不得大于终点，否则 **422**；范围内不存在的批次编号不报错（不产生批次，合计为 0）；
- **未关账流水不计入**结果；查询只读取关账时落库的**不可改快照**（`close_lines`），**历史批次不受后续入账影响**（补录、退款、更正进入下一批后，旧批次返回值不变）；
- 作品视角下不含该作品流水的批次不返回；查询为只读，不改写任何数据，既有接口字段与状态码保持兼容。

```bash
# 查询作品 BOOK-1 全部已冻结批次的关账贡献
curl "http://localhost:8000/works/BOOK-1/close-contributions"

# 仅查第 2~3 批（起止均含）；只给一端亦可（?from_close=2 / ?to_close=3）
curl "http://localhost:8000/works/BOOK-1/close-contributions?from_close=2&to_close=3"
```

响应示例（节选；批次 1 = 销售 1200 册 + 退款 250 册；批次 3 = 净价更正 + 退款 + 价差反向）：

```json
{
  "work_id": "BOOK-1",
  "from_close": 1,
  "to_close": 3,
  "batches": [
    {
      "close_id": 1,
      "request_id": "CLOSE-2026-09",
      "created_at": "2026-10-05T12:00:00+00:00",
      "entry_seq_from": 1,
      "entry_seq_to": 2,
      "sales_count": 1,
      "refunds_count": 1,
      "price_corrections_count": 0,
      "price_delta_reversals_count": 0,
      "sales_copies": 1200,
      "refunds_copies": 250,
      "price_corrections_copies": 0,
      "price_delta_reversals_copies": 0,
      "sales_cents": 333600,
      "refunds_cents": 79950,
      "price_corrections_cents": 0,
      "price_delta_reversals_cents": 0,
      "net_royalty_cents": 253650,
      "lines": [
        {"seq": 1, "entry_seq": 1, "type": "sale", "type_label": "销售",
         "ref_id": "S100", "sale_id": "S100", "split_version": 1,
         "copies": 1200, "amount_cents": 333600, "net_contribution_cents": 333600,
         "created_at": "2026-10-05T09:00:00+00:00"},
        {"seq": 2, "entry_seq": 2, "type": "refund", "type_label": "退款",
         "ref_id": "RF100", "sale_id": "S100", "split_version": 1,
         "copies": 250, "amount_cents": 79950, "net_contribution_cents": -79950,
         "created_at": "2026-10-05T09:10:00+00:00"}
      ]
    },
    {
      "close_id": 3,
      "sales_count": 0,
      "refunds_count": 1,
      "price_corrections_count": 1,
      "price_delta_reversals_count": 1,
      "sales_copies": 0,
      "refunds_copies": 100,
      "price_corrections_copies": 950,
      "price_delta_reversals_copies": 100,
      "sales_cents": 0,
      "refunds_cents": 26700,
      "price_corrections_cents": 50350,
      "price_delta_reversals_cents": 5300,
      "net_royalty_cents": 18350,
      "lines": [
        {"seq": 1, "entry_seq": 4, "type": "price_correction", "type_label": "净价更正",
         "ref_id": "COR-2026-10-001", "sale_id": "S100", "split_version": 1,
         "copies": 950, "amount_cents": 50350, "net_contribution_cents": 50350,
         "created_at": "..."},
        {"seq": 2, "entry_seq": 5, "type": "refund", "type_label": "退款",
         "ref_id": "RF102", "sale_id": "S100", "split_version": 1,
         "copies": 100, "amount_cents": 26700, "net_contribution_cents": -26700,
         "created_at": "..."},
        {"seq": 3, "entry_seq": 6, "type": "price_delta_reversal", "type_label": "价差反向",
         "ref_id": "RF102", "sale_id": "S100", "split_version": 1,
         "copies": 100, "amount_cents": 5300, "net_contribution_cents": -5300,
         "created_at": "..."}
      ]
    }
  ],
  "totals": {
    "batch_count": 3,
    "sales_count": 2,
    "refunds_count": 2,
    "price_corrections_count": 1,
    "price_delta_reversals_count": 1,
    "sales_copies": 1205,
    "refunds_copies": 350,
    "price_corrections_copies": 950,
    "price_delta_reversals_copies": 100,
    "sales_cents": 334600,
    "refunds_cents": 106650,
    "price_corrections_cents": 50350,
    "price_delta_reversals_cents": 5300,
    "net_royalty_cents": 273000
  }
}
```

字段说明：

- 四类流水的 `*_count` / `*_copies` / `*_cents`：批内该作品相应流水的**数量 / 册数 / 金额**；退款金额恒为正（冲减在净额中体现），净价更正与价差反向金额**本身带符号**（补提为正、冲回为负）。
- 净价更正的册数为**更正时各档未退册数之和**；价差反向的册数为**本次退款在各档被退册数之和**。
- `lines[]` 为关账快照中的逐笔摘要，按 `entry_seq` 升序（`seq` 为作品批内序号）；`amount_cents` 为流水原值，`net_contribution_cents` 为对批次净版税的带符号贡献（销售/更正为正，退款/反向为负），每批 `net_royalty_cents` 恰等于各 `net_contribution_cents` 之和。
- `totals`：范围内全部命中批次的跨批合计；无任何关账批次（或范围内无命中）时各字段为 0、`batches` 为空。

| 状态码 | 场景 |
| --- | --- |
| 404 | 作品不存在 |
| 422 | 起始/截止批次为非正整数或非整数；或起始批次大于截止批次 |

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

覆盖：阶梯计提、分账余分补齐、退款跨段冲回与超退拦截、幂等重试与冲突、关账批次归属与报表不可改、付款结算（过期批次拒绝/超付拦截/幂等重放/负余额抵扣/**预约占额拦截直接付款**）、并发入账、关账与结算，作者分成版本（校验/幂等与全局冲突/版本查询/入账版本/旧销售退款按原版本分账/退出作者结算/并发变更与入账/旧库迁移），净价更正（各档未退册数差额与独立分账相减/正负差额/幂等与全局冲突/销售不存在·已全退·同价拒绝/更正后退款的价差反向流水与原字段不变/串联更正与全退逐作者归零/更正与反向进入下一批次且历史不改写/按更正编号查询/并发提交顺序/旧库迁移新流水类型），渠道对账（四类逐笔结论与双方原值/汇总、退款取原销售入账价、更正与反向不参与、空回单与空批次核漏报、重复回单号/重复内部流水/区间越界整批拒绝且不落库、非法数值 422、同批次号同内容重放（顺序无关）与异内容 409、报告冻结不受后续流水影响、不改写销售/退款/关账且不占入账序号、区间窗口与多报原因、批次列表/渠道过滤/404、并发提交），以及付款冲回（整笔冲回与登记时结算快照/有效已付额口径与可付恢复/负余额重算/同号重放与换编号重复冲回 409/原付款 404/空编号 422/后续关账不改旧冲回/按作者核算与查询/并发冲回与付款不超付/并发重复冲回至多一笔），付款预约（占额口径/多笔累计与超额拒绝/直接付款扣除占额/同号重放与异内容冲突/重放于失效后返回原状态/执行登记付款并解除占额/重复执行返回原付款/换付款编号与编号已占用冲突/终态不可转换/取消释放占额并幂等/关账使旧批次 pending 预约失效并释放占额/已执行付款退票仍按现口径/作者·批次·预约 404/过期批次 409/非正金额 422/并发预约不超额/并发执行同一预约至多一笔成功/并发执行不同预约的付款编号唯一），以及作品关账贡献查询（分批四类流水数量/册数/金额与批次净版税口径、流水摘要按入账顺序及带符号贡献、范围合计与起止均含、缺省查全部已冻结批次、作品不存在 404、起止非法/起点大于终点 422、范围越界不报错、未关账流水不计入且历史快照不受后续入账影响、多作品同批隔离、向下更正为负贡献、与冻结关账报表口径一致）。

## 本轮变动（v1.7.0）

- **新增付款预约（实际付款前预占待付额度）**：`POST /payment-reservations` 凭全局唯一预约号、作者编号、当前最新关账批次号、正整数金额（分）预约；`GET /payment-reservations/{reservation_id}` 查询；`POST /payment-reservations/{id}/execute` 执行（全局唯一付款编号）；`POST /payment-reservations/{id}/cancel` 取消。
- **占额口径**：可预约额/可直接付款额 = `max(该批次累计应计 − 有效已付 − 同作者待执行预约占额, 0)`，**负值按零计，超额 400**；**现有直接付款 `/payments` 同步扣除待执行预约占额**，不能绕过预约。执行预约在**同一事务**内校验批次与余额、登记现有口径付款并解除本笔占额。
- **状态机**：`pending → executed / canceled / expired`，终态不得反向转换（409）；重复执行（同付款编号）返回原付款（200），换付款编号执行同一预约、付款编号已被直接付款或其他预约占用均 409；取消幂等（重复取消 200 返回原状态）。
- **关账失效**：新关账时旧批次全部待执行预约统一置为 `expired`、释放占额，关账报表新增 `expired_payment_reservations_count`；**已执行付款及后续退票仍遵循现有记录与结算口径**（退票恢复金额可重新预约/付款）。
- **幂等与报错**：同号同内容创建重试返回原预约状态（200，含已失效情形），同号异内容 409；作者/批次/预约不存在 404，过期批次 409，金额非正 422，非法状态迁移 409。响应返回预约状态、关联付款与作者当前可用额。
- **结算总览新增字段（既有字段与状态码不变）**：`reserved_cents`（待执行预约占额）、`available_cents`（作者当前可用额）、`reservations`（全部状态预约记录）。
- **存储**：新增 `payment_reservations` 表（`CREATE TABLE IF NOT EXISTS`，启动自动建表；旧库补列幂等迁移），不占入账序号、不改写任何既有表。启动方式（`uvicorn app.main:app` / Docker）不变。

## 历史变动（v1.6.0）

- **新增作品关账贡献查询**：`GET /works/{work_id}/close-contributions`，可选 `?from_close=&to_close=`（**起止均含**）。返回每个已冻结关账批次中该作品**销售、退款、净价更正、价差反向**四类流水的**数量、册数与金额**、**批次净版税**（销售 − 退款 + 净价更正 − 价差反向，与关账报表作者净分账同口径）、按**入账顺序**排列的**冻结流水摘要**（含带符号净贡献），以及顶层**范围合计**。
- **口径与边界**：仅已存在作品可查（**404**）；未提供范围查询全部已冻结批次；起止为非正整数或起点大于终点返回 **422**（范围内不存在的批次编号不报错）；**未关账流水不计入**，查询只读取关账落库的不可改快照，**历史批次不受后续入账影响**；作品视角下不含其流水的批次不返回。
- **只读、兼容**：纯只读接口，不新增表、不改写任何数据、不占入账序号；既有接口字段与状态码全部保持不变。启动方式（`uvicorn app.main:app` / Docker）不变。

## 历史变动（v1.5.0）

- **新增付款冲回（银行退票）**：`POST /payment-reversals` 凭**全局唯一冲回编号**与**原付款编号**登记**整笔付款冲回**（不支持部分冲回，冲回金额恒为原付款金额）；`GET /payment-reversals/{reversal_id}` 按冲回编号查询。
- **响应内容**：关联付款（提交时快照）、冲回金额、登记时的作者结算快照（应计、冲回前后有效已付、可付、待抵扣负余额、最新批次）；快照落库后冻结，后续关账不改变旧冲回结果。
- **有效已付额口径**：有效已付额 = 付款总额 − 冲回总额；可付额与待抵扣负余额按最新关账快照实时重算（被退回金额恢复为可付，可按最新批次重新付款，仍不超付）。
- **幂等与唯一性**：每笔付款至多冲回一次（`payment_id` 唯一约束）；同冲回编号同内容重试返回原结果（200），异内容或换编号重复冲回均拒绝（409）；原付款不存在 404，空编号 422；被拒绝的提交不占用冲回编号。
- **不改写历史**：原付款及其提交时快照、既有关账报表均保留；冲回不占用入账序号、不进入关账批次。
- **结算总览新增字段（既有字段与状态码不变）**：`reversed_cents`（已冲回总额）与 `reversals`（冲回记录列表）；`paid_cents` 改为有效已付额口径。
- **并发**：冲回与付款、关账共用同一进程锁与 `BEGIN IMMEDIATE` 单事务，按提交顺序原子判定，并发冲回与付款不会超付，并发重复冲回同一付款至多一笔成功。
- **存储**：新增 `payment_reversals` 表（`CREATE TABLE IF NOT EXISTS`，启动自动建表，无旧库迁移动作）。启动方式（`uvicorn app.main:app` / Docker）不变。

## 历史变动（v1.4.0）

- **新增渠道对账**：`POST /reconciliations` 以全局唯一批次号、入账序号区间 `(from_entry_seq, to_entry_seq]`、渠道与逐笔回单提交；`GET /reconciliations`（可 `?channel=` 过滤）、`GET /reconciliations/{id}`、`GET /reconciliations/by-batch/{batch_no}` 查询冻结报告。
- **核对口径**：区间内仅原销售与原退款参与，净价更正与价差反向流水不参与；退款内部侧册数为退款册数、每册净价恒取原销售入账价（不受后续更正影响）。
- **逐笔结论与双方原值/汇总**：`matched` / `value_mismatch`（`copies_match`、`net_price_match` 指出差异项）/ `channel_extra`（`reason`：`not_found` / `kind_mismatch` / `not_in_interval`）/ `internal_missing`；每行附渠道侧与内部侧册数、净价、金额，顶层附双方销售/退款笔数、册数、金额及净额。
- **空回单核漏报**：回单列表可为空（整批核查内部漏报）；单笔允许 `copies=0` 的空回单，按类型与内部编号参与匹配。
- **整批拒绝**：同一 `(type, ref_id)` 多次出现、回单号批内重复、区间非正长度或终点越过当前最大入账序号 → 400（`detail` 汇总全部原因，不落库，修正后可同号重提）；数值/格式非法 → 422。
- **快照与冻结**：核对与保存在同一数据快照（全局锁 + `BEGIN IMMEDIATE` 单事务）中完成；成功报告整体 JSON 落库后冻结，后续补录、退款、更正均不改旧报告。
- **幂等**：同批次号同内容（回单顺序无关）重试返回原冻结报告（200），异内容 409。
- **只读、不改写、兼容**：对账仅读取销售/退款并写入独立 `reconciliations` 表；不改写销售、退款、关账与付款，不占用入账序号、不影响关账批次；既有接口字段与状态码保持不变。
- **存储**：新增 `reconciliations` 表（`CREATE TABLE IF NOT EXISTS`，启动自动建表，无旧库迁移动作）。启动方式（`uvicorn app.main:app` / Docker）不变。

## 历史变动（v1.3.0）

- **新增销售净价更正**：`POST /price-corrections`（更正编号全局唯一、幂等），`GET /price-corrections/{correction_id}` 按更正编号查询，`GET /price-corrections` 列表（可 `?sale_id=` 过滤）。
- **差额口径**：按**原销售阶梯费率与入账时分成版本**，仅对各档**未退册数**计算新旧每册净价的版税差额（四舍五入到分）；逐作者差额为"新价未退总额分账 − 旧价未退总额分账"（两个总额各自整体分账后相减）。旧价取当前更正价，支持多次串联更正。
- **独立流水、不改历史**：更正以独立入账序号进入**下一关账批次**；历史销售、退款、已关账报表与付款一律不改写。
- **更正后退款**：退款仍按原销售**末册、原价、原分成版本**冲回，**原退款字段及金额不变**；对被退册数另记"已生效价差反向流水"（同事务独立序号、随退款同批次），逐作者按望远镜式差额冲回，**全退后该销售逐作者累计价差归零**；旧退款不追溯。
- **拒绝规则**：销售不存在 404、已全退 400、新价等于当前更正价 409、同号异内容（含跨销售）409、非负校验 422。
- **并发**：更正与退款、关账共用同一进程锁与 `BEGIN IMMEDIATE` 单事务，未退册数与批次归属按数据库提交顺序判定。
- **报表字段（均为新增，既有字段与状态码不变）**：关账报表新增 `price_corrections_count` / `price_delta_reversals_count` / `total_price_correction_cents` / `total_price_delta_reversal_cents` / `total_author_net_cents`；`lines` 新增 `price_correction`、`price_delta_reversal` 两类明细；退款响应新增可空字段 `price_delta_reversal`。
- **存储与迁移**：新增 `price_corrections` / `price_correction_segments` / `price_correction_splits` / `price_delta_reversals` / `price_delta_reversal_segments` / `price_delta_reversal_splits` 表；启动时自动迁移旧库——重建 `entries` 表将 CHECK 约束扩展为含两种新流水类型（保留历史入账序号），无需手工干预。启动方式（`uvicorn app.main:app` / Docker）不变。

## 历史变动（v1.2.0）

- **新增作者分成版本**：`POST /works/{work_id}/splits` 提交新版本（`change_id` 全局唯一幂等），`GET /works/{work_id}/splits`、`GET /works/{work_id}/splits/{version}` 查询当前及历次版本；创建作品时的分成为版本 1。
- **版本生效规则**：新销售按入账时的当前版本分账；旧销售的退款仍从末册冲回并按原销售版本分账；业务时间不决定版本；并发变更与入账由数据库提交顺序确定先后。
- **响应新增字段（均为新增，既有字段与状态码不变）**：作品响应 `split_version`（所展示分成的版本号；`GET` 为当前版本，创建及其重试恒为 1）；销售/退款响应 `split_version`（计提/冲回所用的分成版本）。
- **作者结算**：退出分成的作者仍可查询并结算既有余额；新版本加入的作者可参与后续关账与结算；已关账快照与付款记录不改写。
- **存储与迁移**：新增 `work_versions` / `work_version_authors` 表，`sales` 增加 `split_version` 列；启动时自动迁移旧库——既有作品的创建时分成迁为版本 1，历史销售回填 `split_version = 1`，无需手工干预。

## 项目结构

```
app/
  main.py            # FastAPI 路由
  ledger.py          # 核心账务逻辑（计提/分账/退款/更正/关账/付款/付款预约/付款冲回）
  reconciliation.py  # 渠道逐笔对账（快照核对、冻结报告、幂等）
  db.py              # SQLite 连接与 schema 初始化
  schemas.py         # 请求模型与校验
tests/test_api.py
tests/test_reconciliation.py
tests/test_payment_reservations.py
tests/test_payment_reversals.py
tests/test_close_contributions.py
examples/demo.sh
Dockerfile / docker-compose.yml
```
