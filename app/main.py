"""FastAPI 应用与路由。"""
from __future__ import annotations

from contextlib import asynccontextmanager
import math
from decimal import Decimal

from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import ledger, reconciliation
from .db import init_db
from .ledger import ApiError
from .schemas import (
    CloseIn,
    PaymentIn,
    PaymentReversalIn,
    PriceCorrectionIn,
    ReconciliationIn,
    RefundIn,
    SaleIn,
    SplitChangeIn,
    WorkIn,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # 启动时自动初始化数据库
    yield


app = FastAPI(
    title="数字出版版税结算服务",
    version="1.6.0",
    description="阶梯版税计提、作者分成版本、退款冲销、净价更正、关账报表、关账后付款结算、付款冲回（银行退票）、渠道逐笔对账与作品关账贡献查询",
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(request, exc: ApiError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # pydantic 拦截 NaN/Infinity 时，错误上下文中的 Decimal('NaN') 无法被默认 JSON 序列化，
    # 这里统一转为可序列化的安全形式（如字符串 "NaN"），保证非法数值稳定返回 422
    def safe(v):
        # Decimal（含 NaN/Infinity）无法被标准 JSON 序列化，统一转规范字符串；
        # 非有限 float 同样转字符串；复杂容器递归处理，其余原样保留
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, float) and not math.isfinite(v):
            return str(v)
        if isinstance(v, dict):
            return {k: safe(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [safe(x) for x in v]
        return v

    detail = [
        {
            "loc": list(e.get("loc", [])),
            "msg": e.get("msg", ""),
            "type": e.get("type", ""),
            "input": safe(e.get("input")),
        }
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


@app.get("/", summary="服务信息")
def root():
    return {"service": "数字出版版税结算服务", "version": "1.6.0", "docs": "/docs"}


@app.get("/health", summary="健康检查")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 作品


@app.post("/works", status_code=201, summary="录入作品（阶梯费率 + 作者分成）")
def create_work(payload: WorkIn, response: Response):
    body, created = ledger.create_work(payload)
    if not created:  # 同号同内容重试 → 返回原结果
        response.status_code = 200
    return body


@app.get("/works/{work_id}", summary="查询作品")
def get_work(work_id: str):
    return ledger.get_work(work_id)


@app.get("/works/{work_id}/stats", summary="查询作品销量汇总")
def get_work_stats(work_id: str):
    return ledger.work_stats(work_id)


@app.get(
    "/works/{work_id}/close-contributions",
    summary="作品关账贡献查询（分批销售/退款/净价更正/价差反向数量、册数、金额与批次净版税，含冻结流水摘要与范围合计）",
)
def get_work_close_contributions(
    work_id: str,
    from_close: int | None = Query(default=None, ge=1, description="起始关账批次编号（含）"),
    to_close: int | None = Query(default=None, ge=1, description="截止关账批次编号（含）"),
):
    return ledger.work_close_contributions(work_id, from_close, to_close)


# ---------------------------------------------------------------- 作者分成版本


@app.post(
    "/works/{work_id}/splits",
    status_code=201,
    summary="提交作者分成新版本（变更编号幂等，按提交顺序生效）",
)
def create_split_version(work_id: str, payload: SplitChangeIn, response: Response):
    body, created = ledger.submit_split_change(work_id, payload)
    if not created:  # 同变更编号同内容重试 → 返回原版本
        response.status_code = 200
    return body


@app.get("/works/{work_id}/splits", summary="查询作品当前及历次分成版本")
def list_split_versions(work_id: str):
    return ledger.list_split_versions(work_id)


@app.get("/works/{work_id}/splits/{version}", summary="查询作品指定分成版本")
def get_split_version(work_id: str, version: int):
    return ledger.get_split_version(work_id, version)


# ---------------------------------------------------------------- 销售


@app.post("/sales", status_code=201, summary="登记销售流水（入账即计提版税）")
def create_sale(payload: SaleIn, response: Response):
    body, created = ledger.record_sale(payload)
    if not created:
        response.status_code = 200
    return body


@app.get("/sales/{sale_id}", summary="查询销售流水及计提明细")
def get_sale(sale_id: str):
    return ledger.get_sale(sale_id)


# ---------------------------------------------------------------- 退款


@app.post("/refunds", status_code=201, summary="登记退款流水（从最后一册起冲回）")
def create_refund(payload: RefundIn, response: Response):
    body, created = ledger.record_refund(payload)
    if not created:
        response.status_code = 200
    return body


@app.get("/refunds/{refund_id}", summary="查询退款流水及冲销明细")
def get_refund(refund_id: str):
    return ledger.get_refund(refund_id)


# ---------------------------------------------------------------- 净价更正


@app.post(
    "/price-corrections",
    status_code=201,
    summary="提交销售净价更正（仅对未退册数补提/冲回版税差额）",
)
def create_price_correction(payload: PriceCorrectionIn, response: Response):
    body, created = ledger.submit_price_correction(payload)
    if not created:  # 同更正编号同内容重试 → 返回原结果
        response.status_code = 200
    return body


@app.get("/price-corrections", summary="查询净价更正列表（可按 sale_id 过滤）")
def list_price_corrections(sale_id: str | None = None):
    return ledger.list_price_corrections(sale_id)


@app.get("/price-corrections/{correction_id}", summary="按更正编号查询净价更正")
def get_price_correction(correction_id: str):
    return ledger.get_price_correction(correction_id)


# ---------------------------------------------------------------- 关账


@app.post("/closes", status_code=201, summary="关账：汇总本批流水，生成不可改报表")
def create_close(response: Response, payload: CloseIn | None = None):
    body, created = ledger.close_batch(payload.request_id if payload else None)
    if not created:
        response.status_code = 200
    return body


@app.get("/closes", summary="关账批次列表")
def list_closes():
    return ledger.list_closes()


@app.get("/closes/{close_id}", summary="查询关账报表（不可改）")
def get_close(close_id: int):
    return ledger.get_close(close_id)


# ---------------------------------------------------------------- 付款结算


@app.post("/payments", status_code=201, summary="提交作者付款结算（仅针对当前最新关账批次）")
def create_payment(payload: PaymentIn, response: Response):
    body, created = ledger.submit_payment(payload)
    if not created:  # 同号同内容重试 → 返回原结算结果
        response.status_code = 200
    return body


@app.get("/payments/{payment_id}", summary="查询单笔付款结算")
def get_payment(payment_id: str):
    return ledger.get_payment(payment_id)


# ---------------------------------------------------------------- 付款冲回（银行退票）


@app.post(
    "/payment-reversals",
    status_code=201,
    summary="登记付款冲回（银行退票；全局唯一冲回编号，整笔冲回）",
)
def create_payment_reversal(payload: PaymentReversalIn, response: Response):
    body, created = ledger.submit_payment_reversal(payload)
    if not created:  # 同冲回编号同内容重试 → 返回原结果
        response.status_code = 200
    return body


@app.get("/payment-reversals/{reversal_id}", summary="按冲回编号查询付款冲回")
def get_payment_reversal(reversal_id: str):
    return ledger.get_payment_reversal(reversal_id)


@app.get("/authors/{author_id}/settlement", summary="作者结算总览（分批应计/已付/负余额/可付/付款记录）")
def get_author_settlement(author_id: str):
    return ledger.author_settlement(author_id)


# ---------------------------------------------------------------- 渠道对账


@app.post(
    "/reconciliations",
    status_code=201,
    summary="提交渠道对账批次（批次号幂等；区间内仅原销售/退款，退款取原销售入账价）",
)
def create_reconciliation(payload: ReconciliationIn, response: Response):
    body, created = reconciliation.submit_reconciliation(payload)
    if not created:  # 同批次号同内容重试 → 返回原冻结报告
        response.status_code = 200
    return body


@app.get("/reconciliations", summary="对账批次列表（可按渠道过滤）")
def list_reconciliations(channel: str | None = None):
    return reconciliation.list_reconciliations(channel)


@app.get(
    "/reconciliations/by-batch/{batch_no}",
    summary="按批次号查询冻结对账报告",
)
def get_reconciliation_by_batch(batch_no: str):
    return reconciliation.get_reconciliation_by_batch(batch_no)


@app.get("/reconciliations/{reconciliation_id}", summary="按自增编号查询冻结对账报告")
def get_reconciliation(reconciliation_id: int):
    return reconciliation.get_reconciliation(reconciliation_id)
