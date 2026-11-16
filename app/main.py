"""FastAPI 入口：上传三表 JSON、运行只读聚合 SQL、返回行级溯源结果。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import storage
from .lineage import LineageError, run
from .sqlguard import QueryError, validate
from .storage import UploadError

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
SAMPLE_PATH = BASE_DIR / "sample" / "sample_data.json"

SAMPLE_QUERIES = [
    {
        "name": "LEFT 双连接：按订单汇总（暴露重复计入）",
        "sql": (
            "SELECT o.order_id, SUM(o.amount) AS total_amount\n"
            "FROM orders o\n"
            "LEFT JOIN payments p ON o.order_id = p.order_id\n"
            "LEFT JOIN refunds r ON o.order_id = r.order_id\n"
            "GROUP BY o.order_id"
        ),
    },
    {
        "name": "INNER 连接：按渠道汇总",
        "sql": (
            "SELECT o.channel, SUM(o.amount) AS total_amount\n"
            "FROM orders o\n"
            "INNER JOIN payments p ON o.order_id = p.order_id\n"
            "GROUP BY o.channel"
        ),
    },
]

app = FastAPI(
    title="结算订单金额溯源",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.exception_handler(UploadError)
async def upload_error_handler(_: Request, exc: UploadError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(QueryError)
async def query_error_handler(_: Request, exc: QueryError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(LineageError)
async def lineage_error_handler(_: Request, exc: LineageError) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": f"溯源校验失败：{exc}"})


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": f"服务器内部错误：{exc}"})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/sample")
def sample() -> dict:
    data = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    return {"data": data, "queries": SAMPLE_QUERIES}


@app.post("/api/upload")
async def upload(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        raise UploadError("请求体不是合法 JSON")
    return storage.load_dataset(payload)


@app.get("/api/dataset")
def dataset() -> dict:
    ds = storage.get_current()
    return {"loaded": ds is not None, "summary": ds.summary() if ds else None}


@app.post("/api/query")
async def query(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise QueryError("请求体不是合法 JSON")
    if not isinstance(body, dict) or not isinstance(body.get("sql"), str):
        raise QueryError("请求体必须是 JSON 对象且包含字符串字段 sql")
    ds = storage.require_current()
    vq = validate(body["sql"], ds.schemas)
    return run(ds, vq)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
