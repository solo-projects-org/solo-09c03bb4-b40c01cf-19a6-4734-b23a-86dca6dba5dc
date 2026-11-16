"""上传数据校验与 SQLite 装载。

数据契约（三表 JSON）：
- orders   每行必含 order_id（非空）与 amount（非空数值）
- payments 每行必含 pay_id（非空且表内唯一）与 order_id（非空）
- refunds  每行必含 refund_id（非空且表内唯一）与 order_id（非空）
- 三表总行数 <= 500；允许额外标量字段（如 orders.channel）
"""
from __future__ import annotations

import math
import re
import sqlite3
import threading
from dataclasses import dataclass
from typing import Sequence

MAX_TOTAL_ROWS = 500

TABLES = ("orders", "payments", "refunds")
REQUIRED_COLUMNS = {
    "orders": ("order_id", "amount"),
    "payments": ("pay_id", "order_id"),
    "refunds": ("refund_id", "order_id"),
}
UNIQUE_COLUMN = {"payments": "pay_id", "refunds": "refund_id"}
RESERVED_NAMES = {"rowid", "oid", "_rowid_"}
COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class UploadError(Exception):
    """上传数据不合法。"""


@dataclass
class Dataset:
    conn: sqlite3.Connection
    schemas: dict[str, list[str]]  # 表名 -> 有序字段名
    counts: dict[str, int]

    def summary(self) -> dict:
        return {
            "tables": {
                name: {"rows": self.counts[name], "columns": self.schemas[name]}
                for name in TABLES
            },
            "total_rows": sum(self.counts.values()),
            "max_total_rows": MAX_TOTAL_ROWS,
        }


_lock = threading.Lock()
_current: Dataset | None = None


def get_current() -> Dataset | None:
    with _lock:
        return _current


def require_current() -> Dataset:
    ds = get_current()
    if ds is None:
        raise UploadError("尚未上传数据，请先上传三表 JSON")
    return ds


def execute(ds: Dataset, sql: str, params: Sequence = ()) -> tuple[list[str], list[tuple]]:
    """在当前数据集上执行只读 SQL，返回 (列名, 行)。"""
    with _lock:
        cur = ds.conn.execute(sql, tuple(params))
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    return cols, rows


def _is_num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_id(v: object) -> bool:
    if isinstance(v, bool) or v is None:
        return False
    if isinstance(v, float):
        return math.isfinite(v)
    return isinstance(v, (str, int))


def _is_scalar(v: object) -> bool:
    return v is None or isinstance(v, (str, int, float, bool))


def _validate_table(name: str, rows: object) -> tuple[list[str], list[dict]]:
    if not isinstance(rows, list):
        raise UploadError(f"表 {name} 必须是 JSON 数组")
    required = REQUIRED_COLUMNS[name]
    columns: list[str] = []
    by_lower: dict[str, str] = {}
    for idx, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise UploadError(f"表 {name} 第 {idx} 行不是 JSON 对象")
        for col, val in row.items():
            if not COLUMN_RE.match(col):
                raise UploadError(
                    f"表 {name} 存在非法字段名 {col!r}（仅允许字母/数字/下划线，且不以数字开头）"
                )
            low = col.lower()
            if low in RESERVED_NAMES or low.startswith("__"):
                raise UploadError(f"表 {name} 字段名 {col!r} 为系统保留字，禁止使用")
            prev = by_lower.get(low)
            if prev is None:
                by_lower[low] = col
                columns.append(col)
            elif prev != col:
                raise UploadError(f"表 {name} 字段 {col!r} 与 {prev!r} 仅大小写不同，无法区分")
            if not _is_scalar(val):
                raise UploadError(f"表 {name} 第 {idx} 行字段 {col!r} 的值必须是标量（字符串/数字/布尔/null）")
        for req in required:
            if req not in row:
                raise UploadError(f"表 {name} 第 {idx} 行缺少必填字段 {req!r}")
        if name == "orders":
            if not _is_id(row["order_id"]):
                raise UploadError(f"表 orders 第 {idx} 行 order_id 必须是非空字符串或数字")
            if not _is_num(row["amount"]):
                raise UploadError(f"表 orders 第 {idx} 行 amount 必须是非空数值")
        else:
            id_col = UNIQUE_COLUMN[name]
            if not _is_id(row[id_col]):
                raise UploadError(f"表 {name} 第 {idx} 行 {id_col} 必须是非空字符串或数字")
            if not _is_id(row["order_id"]):
                raise UploadError(f"表 {name} 第 {idx} 行 order_id 必须是非空字符串或数字")
    if not rows:  # 空表退化为必填字段结构，保证建表成功
        columns = list(required)
    if name in UNIQUE_COLUMN:
        id_col = UNIQUE_COLUMN[name]
        seen: set = set()
        for idx, row in enumerate(rows, 1):
            v = row[id_col]
            if v in seen:
                raise UploadError(f"表 {name} 第 {idx} 行 {id_col}={v!r} 重复，行号必须唯一")
            seen.add(v)
    return columns, rows


def load_dataset(payload: object) -> dict:
    """校验并装载三表 JSON，返回数据集摘要。失败抛 UploadError，不改动现有数据。"""
    global _current
    if not isinstance(payload, dict):
        raise UploadError('请求体必须是 JSON 对象：{"orders": [...], "payments": [...], "refunds": [...]}')
    unknown = sorted(set(payload) - set(TABLES))
    if unknown:
        raise UploadError(f"存在未知表 {unknown}，仅支持 orders / payments / refunds")
    missing = [t for t in TABLES if t not in payload]
    if missing:
        raise UploadError(f"缺少表 {missing}，需同时提供 orders / payments / refunds")

    parsed: dict[str, tuple[list[str], list[dict]]] = {}
    total = 0
    for t in TABLES:
        cols, rows = _validate_table(t, payload[t])
        parsed[t] = (cols, rows)
        total += len(rows)
    if total > MAX_TOTAL_ROWS:
        raise UploadError(f"三表总行数 {total} 超过上限 {MAX_TOTAL_ROWS}")

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schemas: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    try:
        for t in TABLES:
            cols, rows = parsed[t]
            col_sql = ", ".join(f'"{c}"' for c in cols)
            conn.execute(f'CREATE TABLE "{t}" ({col_sql})')
            if rows:
                ph = ", ".join("?" for _ in cols)
                conn.executemany(
                    f'INSERT INTO "{t}" VALUES ({ph})',
                    [[r.get(c) for c in cols] for r in rows],
                )
            schemas[t] = cols
            counts[t] = len(rows)
        conn.commit()
    except Exception:
        conn.close()
        raise
    ds = Dataset(conn=conn, schemas=schemas, counts=counts)
    with _lock:
        old = _current
        _current = ds
    if old is not None:
        old.conn.close()
    return ds.summary()
