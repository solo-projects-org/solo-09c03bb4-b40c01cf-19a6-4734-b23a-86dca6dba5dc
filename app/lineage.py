"""溯源查询构建与执行。

原理：不改动原查询语义，仅基于同一 FROM/JOIN/WHERE 生成两条派生查询——
1) 贡献查询：按 分组字段 + 订单行(rowid) 细分聚合，得到每单计入次数 COUNT(*)
   与贡献金额 SUM(amount)；各订单贡献之和必然等于该组实际汇总（运行时会校验）。
2) 证据查询：去掉聚合，逐行列出参与汇总的所有关联行组合（orders/payments/
   refunds 的 rowid），再按 rowid 回查原始行作为证据。
LEFT JOIN 空行、NULL 过滤、重复匹配均由 SQLite 按原 SQL 语义自然得出。
"""
from __future__ import annotations

import sqlite3

from sqlglot import exp

from . import storage
from .sqlguard import (
    AMOUNT_COLUMN,
    MAIN_TABLE,
    ORDER_ID_COLUMN,
    QueryError,
    ValidatedQuery,
)

EVIDENCE_LIMIT = 2000  # 行级证据返回上限（汇总与贡献数值不受截断影响）
_TOL = 1e-6


class LineageError(Exception):
    """溯源完整性校验失败（内部不一致，不展示推测归因）。"""


def _col(alias: str, name: str) -> exp.Column:
    return exp.column(name, table=alias)


def build_contrib_query(vq: ValidatedQuery) -> str:
    """按 分组字段 + 订单行 细分的聚合：每单计入次数与贡献金额。"""
    oa = vq.alias_of(MAIN_TABLE)
    q = vq.select.copy()
    selects = [_col(vq.alias_of(c.table), c.column) for c in vq.group_cols]
    selects += [
        exp.alias_(_col(oa, "rowid"), "__order_rowid"),
        exp.alias_(_col(oa, ORDER_ID_COLUMN), "__order_id"),
        exp.alias_(_col(oa, AMOUNT_COLUMN), "__amount"),
        exp.alias_(exp.Count(this=exp.Star()), "__cnt"),
        exp.alias_(exp.Sum(this=_col(oa, AMOUNT_COLUMN)), "__contrib"),
    ]
    groups = [_col(vq.alias_of(c.table), c.column) for c in vq.group_cols]
    groups += [_col(oa, "rowid"), _col(oa, ORDER_ID_COLUMN), _col(oa, AMOUNT_COLUMN)]
    q.set("expressions", selects)
    q.set("group", exp.Group(expressions=groups))
    return q.sql(dialect="sqlite")


def build_evidence_query(vq: ValidatedQuery) -> str:
    """逐行列出参与汇总的关联行组合（各表 rowid）。"""
    oa = vq.alias_of(MAIN_TABLE)
    q = vq.select.copy()
    selects = [_col(vq.alias_of(c.table), c.column) for c in vq.group_cols]
    selects.append(exp.alias_(_col(oa, "rowid"), "__order_rowid"))
    if "payments" in vq.joined_tables:
        selects.append(exp.alias_(_col(vq.alias_of("payments"), "rowid"), "__payment_rowid"))
    if "refunds" in vq.joined_tables:
        selects.append(exp.alias_(_col(vq.alias_of("refunds"), "rowid"), "__refund_rowid"))
    order_cols = [_col(vq.alias_of(c.table), c.column) for c in vq.group_cols]
    order_cols.append(_col(oa, "rowid"))
    q.set("expressions", selects)
    q.set("group", None)
    q.set("order", exp.Order(expressions=[exp.Ordered(this=c) for c in order_cols]))
    q.set("limit", exp.Limit(expression=exp.Literal.number(EVIDENCE_LIMIT + 1)))
    return q.sql(dialect="sqlite")


def _fetch_rows(ds: storage.Dataset, table: str, rowids: set[int]) -> dict:
    if not rowids:
        return {}
    ids = sorted(rowids)
    ph = ",".join("?" for _ in ids)
    cols, rows = storage.execute(
        ds, f'SELECT rowid, * FROM "{table}" WHERE rowid IN ({ph})', ids
    )
    out: dict[str, dict] = {}
    for row in rows:
        out[str(row[0])] = {cols[i]: row[i] for i in range(1, len(cols))}
    return out


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= _TOL * max(1.0, abs(a), abs(b))


def run(ds: storage.Dataset, vq: ValidatedQuery) -> dict:
    n_group = len(vq.group_cols)
    try:
        _, orig_rows = storage.execute(ds, vq.sql)
        _, contrib_rows = storage.execute(ds, build_contrib_query(vq))
        _, ev_rows = storage.execute(ds, build_evidence_query(vq))
    except sqlite3.Error as e:
        raise QueryError(f"SQL 执行失败：{e}") from e

    # 1) 原查询：分组 -> 实际汇总
    groups: dict[tuple, dict] = {}
    order_seq: list[tuple] = []
    for row in orig_rows:
        key = tuple(row[p] for p in vq.group_pos)
        total = row[vq.sum_pos]
        if key not in groups:
            groups[key] = {"key": list(key), "total": total, "orders": []}
            order_seq.append(key)
        elif groups[key]["total"] != total:
            raise LineageError("原查询同一分组出现多行且汇总不一致")

    # 2) 贡献查询：分组 -> 订单 -> 计入次数/贡献金额
    for row in contrib_rows:
        key = tuple(row[:n_group])
        g = groups.get(key)
        if g is None:
            raise LineageError("溯源分组与原查询分组不一致")
        order_rowid, order_id, amount, cnt, contrib = row[n_group : n_group + 5]
        g["orders"].append(
            {
                "order_rowid": order_rowid,
                "order_id": order_id,
                "amount": amount,
                "count": cnt,
                "contribution": contrib,
                "duplicated": cnt > 1,
            }
        )

    # 3) 完整性校验：各订单贡献之和必须等于该组实际汇总，否则报错而非推测
    for key in order_seq:
        g = groups[key]
        g["orders"].sort(key=lambda o: o["order_rowid"])
        contrib_sum = sum(o["contribution"] for o in g["orders"])
        total = g["total"]
        if total is None or not _close(contrib_sum, total):
            raise LineageError(
                f"分组 {g['key']} 的订单贡献合计 {contrib_sum} 与实际汇总 {total} 不一致"
            )
        g["contrib_sum"] = contrib_sum
        g["check_ok"] = True

    # 4) 证据查询：关联行组合（rowid 三元组），超上限截断并标记
    truncated = len(ev_rows) > EVIDENCE_LIMIT
    if truncated:
        ev_rows = ev_rows[:EVIDENCE_LIMIT]
    has_p = "payments" in vq.joined_tables
    has_r = "refunds" in vq.joined_tables
    evidence: list[dict] = []
    p_rowids: set[int] = set()
    r_rowids: set[int] = set()
    for row in ev_rows:
        entry = {"group_key": list(row[:n_group]), "order_rowid": row[n_group]}
        i = n_group + 1
        if has_p:
            entry["payment_rowid"] = row[i]
            if row[i] is not None:
                p_rowids.add(row[i])
            i += 1
        if has_r:
            entry["refund_rowid"] = row[i]
            if row[i] is not None:
                r_rowids.add(row[i])
            i += 1
        evidence.append(entry)

    # 5) 按 rowid 回查原始行作为证据
    order_rowids = {o["order_rowid"] for key in order_seq for o in groups[key]["orders"]}
    records = {
        "orders": _fetch_rows(ds, "orders", order_rowids),
        "payments": _fetch_rows(ds, "payments", p_rowids) if has_p else {},
        "refunds": _fetch_rows(ds, "refunds", r_rowids) if has_r else {},
    }

    return {
        "sql": vq.sql,
        "group_columns": [c.label for c in vq.group_cols],
        "sum_label": vq.sum_label,
        "joined_tables": vq.joined_tables,
        "groups": [groups[k] for k in order_seq],
        "evidence": {"truncated": truncated, "limit": EVIDENCE_LIMIT, "rows": evidence},
        "records": records,
    }
