"""关键功能自测：上传校验、SQL 白名单、行级溯源与完整性。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import storage
from app.main import app

client = TestClient(app)

SAMPLE = json.loads(
    (Path(__file__).resolve().parent.parent / "sample" / "sample_data.json").read_text(
        encoding="utf-8"
    )
)

LEFT_SQL = (
    "SELECT o.order_id, SUM(o.amount) AS total_amount "
    "FROM orders o "
    "LEFT JOIN payments p ON o.order_id = p.order_id "
    "LEFT JOIN refunds r ON o.order_id = r.order_id "
    "GROUP BY o.order_id"
)
INNER_CHANNEL_SQL = (
    "SELECT o.channel, SUM(o.amount) AS total_amount "
    "FROM orders o "
    "INNER JOIN payments p ON o.order_id = p.order_id "
    "GROUP BY o.channel"
)


@pytest.fixture(autouse=True)
def upload_sample():
    r = client.post("/api/upload", json=SAMPLE)
    assert r.status_code == 200, r.text
    return r.json()


def run_query(sql: str):
    return client.post("/api/query", json={"sql": sql})


def group_by_key(data: dict) -> dict:
    return {tuple(g["key"]): g for g in data["groups"]}


# ---------- 上传 ----------

def test_upload_summary(upload_sample):
    assert upload_sample["total_rows"] == 16
    assert upload_sample["tables"]["orders"]["rows"] == 5
    assert "channel" in upload_sample["tables"]["orders"]["columns"]
    assert upload_sample["tables"]["payments"]["rows"] == 7


def test_sample_endpoint():
    r = client.get("/api/sample")
    assert r.status_code == 200
    body = r.json()
    assert set(body["data"]) == {"orders", "payments", "refunds"}
    assert len(body["queries"]) >= 2


def test_index_page():
    r = client.get("/")
    assert r.status_code == 200
    assert "结算订单金额溯源" in r.text


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda d: d.pop("orders"), "缺少表"),
        (lambda d: d.update(extra=[]), "未知表"),
        (lambda d: d.update(orders={}), "数组"),
        (lambda d: d["orders"][0].update(amount=None), "amount"),
        (lambda d: d["orders"][0].update(amount="abc"), "amount"),
        (lambda d: d["orders"][0].update(amount=True), "amount"),
        (lambda d: d["orders"][0].pop("order_id"), "order_id"),
        (lambda d: d["payments"][0].update(pay_id="P9002"), "唯一"),
        (lambda d: d["payments"][0].update(pay_id=None), "pay_id"),
        (lambda d: d["refunds"][0].update(**{"refund-id": "X"}), "非法字段名"),
        (lambda d: d["orders"][0].update(rowid=1), "保留"),
        (lambda d: d["orders"][0].update(extra={"a": 1}), "标量"),
        (lambda d: d.update(orders=d["orders"] * 101), "上限"),
    ],
)
def test_upload_rejected(mutate, fragment):
    payload = json.loads(json.dumps(SAMPLE))
    mutate(payload)
    r = client.post("/api/upload", json=payload)
    assert r.status_code == 400, r.text
    assert fragment in r.json()["detail"]


def test_query_before_upload():
    storage._current = None
    try:
        r = run_query(LEFT_SQL)
        assert r.status_code == 400
        assert "尚未上传" in r.json()["detail"]
    finally:
        client.post("/api/upload", json=SAMPLE)


# ---------- SQL 白名单 ----------

@pytest.mark.parametrize(
    "sql, fragment",
    [
        ("SELECT o.order_id, SUM(o.amount) FROM orders o GROUP BY o.order_id ORDER BY 1", "ORDER BY"),
        ("SELECT o.order_id, SUM(o.amount) FROM orders o GROUP BY o.order_id LIMIT 5", "LIMIT"),
        ("SELECT o.order_id, SUM(o.amount) FROM orders o GROUP BY o.order_id HAVING SUM(o.amount) > 1", "HAVING"),
        ("SELECT DISTINCT o.order_id, SUM(o.amount) FROM orders o GROUP BY o.order_id", "DISTINCT"),
        ("SELECT o.order_id, SUM(DISTINCT o.amount) FROM orders o GROUP BY o.order_id", "DISTINCT"),
        ("SELECT o.order_id, SUM(o.amount) OVER () FROM orders o GROUP BY o.order_id", "窗口"),
        ("SELECT o.order_id, SUM(o.amount) FROM orders o WHERE o.order_id IN (SELECT order_id FROM payments) GROUP BY o.order_id", "子查询"),
        ("SELECT o.order_id, SUM(o.amount) FROM (SELECT * FROM orders) o GROUP BY o.order_id", "子查询"),
        ("WITH x AS (SELECT 1) SELECT o.order_id, SUM(o.amount) FROM orders o GROUP BY o.order_id", "CTE"),
        ("SELECT o.order_id, SUM(o.amount) FROM orders o RIGHT JOIN payments p ON o.order_id = p.order_id GROUP BY o.order_id", "INNER/LEFT"),
        ("SELECT o.order_id, SUM(o.amount) FROM orders o CROSS JOIN payments p GROUP BY o.order_id", "INNER/LEFT"),
        ("SELECT o.order_id, SUM(o.amount) FROM orders o, payments p GROUP BY o.order_id", "逗号"),
        ("SELECT o.order_id, SUM(o.amount) FROM orders o JOIN payments p GROUP BY o.order_id", "ON"),
        ("SELECT o.order_id, SUM(o.amount) FROM orders o JOIN payments p USING (order_id) GROUP BY o.order_id", "USING"),
        ("SELECT order_id, SUM(amount) FROM orders GROUP BY order_id", "别名"),
        ("SELECT o.order_id, SUM(o.amount) FROM orders o GROUP BY 1", "GROUP BY 仅支持字段"),
        ("SELECT o.order_id, SUM(o.amount) FROM orders o GROUP BY o.order_id + 1", "GROUP BY 仅支持字段"),
        ("SELECT o.*, SUM(o.amount) FROM orders o GROUP BY o.order_id", "*"),
        ("SELECT o.order_id, COUNT(*) FROM orders o GROUP BY o.order_id", "不支持"),
        ("SELECT o.order_id, SUM(o.order_id) FROM orders o GROUP BY o.order_id", "金额"),
        ("SELECT o.order_id, SUM(o.amount) FROM orders o", "GROUP BY"),
        ("SELECT o.order_id, SUM(o.amount) FROM orders o GROUP BY o.channel", "GROUP BY"),
        ("SELECT o.order_id, SUM(o.amount) FROM orders o INNER JOIN payments p ON o.order_id = p.order_id GROUP BY order_id", "歧义"),
        ("INSERT INTO orders VALUES (1, 2)", "SELECT"),
        ("DELETE FROM orders", "SELECT"),
        ("SELECT 1; SELECT 2", "单条"),
        ("SELECT o.order_id, SUM(o.amount) FROM payments o GROUP BY o.order_id", "orders"),
        ("SELECT o.order_id, SUM(o.amount) FROM orders o INNER JOIN orders o2 ON o.order_id = o2.order_id GROUP BY o.order_id", "只能出现一次"),
        ("SELECT o.order_id, SUM(o.amount) FROM orders o INNER JOIN payments p ON o.order_id = p.order_id INNER JOIN payments p2 ON o.order_id = p2.order_id GROUP BY o.order_id", "重复 JOIN"),
        ("SELECT o.order_id, SUM(o.amount) FROM orders o WHERE UPPER(o.order_id) = 'A' GROUP BY o.order_id", "不支持"),
        ("SELECT o.order_id, SUM(o.amount) FROM orders o WHERE o.no_such = 1 GROUP BY o.order_id", "不存在字段"),
        ("SELECT o.order_id, SUM(o.amount), SUM(o.amount) FROM orders o GROUP BY o.order_id", "一个 SUM"),
        ("SELECT o.order_id, SUM(o.amount) FROM orders o GROUP BY o.order_id; DROP TABLE orders", "单条"),
    ],
)
def test_sql_rejected(sql, fragment):
    r = run_query(sql)
    assert r.status_code == 400, f"{sql} -> {r.status_code}"
    assert fragment in r.json()["detail"], r.json()["detail"]
    assert "groups" not in r.json()


# ---------- 行级溯源 ----------

def test_left_join_lineage():
    r = run_query(LEFT_SQL)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["group_columns"] == ["o.order_id"]
    assert data["joined_tables"] == ["payments", "refunds"]
    g = group_by_key(data)
    # 2 支付 × 1 退款 = 计入 2 次
    assert g[("A1001",)]["total"] == 200.0
    assert g[("A1001",)]["orders"][0]["count"] == 2
    assert g[("A1001",)]["orders"][0]["duplicated"] is True
    # 2 支付 × 2 退款 = 计入 4 次（典型虚高）
    assert g[("A1004",)]["total"] == 800.0
    o4 = g[("A1004",)]["orders"][0]
    assert o4["count"] == 4 and o4["contribution"] == 800.0 and o4["duplicated"]
    # 无支付无退款：LEFT JOIN 空行计入 1 次
    o5 = g[("A1005",)]["orders"][0]
    assert o5["count"] == 1 and o5["contribution"] == 88.0 and not o5["duplicated"]
    # 每组：各订单贡献之和 == 实际汇总
    for grp in data["groups"]:
        assert grp["check_ok"] is True
        assert abs(sum(o["contribution"] for o in grp["orders"]) - grp["total"]) < 1e-6
    # 证据行数 == 各订单计入次数之和（未截断）
    assert data["evidence"]["truncated"] is False
    total_cnt = sum(o["count"] for grp in data["groups"] for o in grp["orders"])
    assert len(data["evidence"]["rows"]) == total_cnt
    # A1005 的证据为外连接空行
    rows5 = [e for e in data["evidence"]["rows"] if e["group_key"] == ["A1005"]]
    assert len(rows5) == 1
    assert rows5[0]["payment_rowid"] is None and rows5[0]["refund_rowid"] is None
    # 证据可回查原行
    rec = data["records"]["orders"][str(o4["order_rowid"])]
    assert rec["order_id"] == "A1004" and rec["amount"] == 200.0


def test_same_amount_orders_distinguished():
    data = run_query(LEFT_SQL).json()
    g = group_by_key(data)
    o1 = g[("A1001",)]["orders"][0]
    o2 = g[("A1002",)]["orders"][0]
    assert o1["amount"] == o2["amount"] == 100.0
    assert o1["order_rowid"] != o2["order_rowid"]
    assert o1["count"] == 2 and o2["count"] == 1  # 同额不同单，分别归因


def test_inner_join_filters_unmatched():
    data = run_query(INNER_CHANNEL_SQL).json()
    g = group_by_key(data)
    # A1005 无支付被 INNER JOIN 过滤；孤儿支付 P9006 无订单也不出现
    assert set(g) == {("APP",), ("WEB",)}
    assert g[("APP",)]["total"] == 700.0  # 200 + 100 + 400
    assert g[("WEB",)]["total"] == pytest.approx(59.9)
    app_orders = {o["order_id"] for o in g[("APP",)]["orders"]}
    assert app_orders == {"A1001", "A1002", "A1004"}


def test_where_null_filter():
    sql = (
        "SELECT o.order_id, SUM(o.amount) AS t FROM orders o "
        "LEFT JOIN payments p ON o.order_id = p.order_id "
        "WHERE p.pay_id IS NULL GROUP BY o.order_id"
    )
    data = run_query(sql).json()
    g = group_by_key(data)
    assert set(g) == {("A1005",)}
    assert g[("A1005",)]["total"] == 88.0
    assert g[("A1005",)]["orders"][0]["count"] == 1


def test_where_in_and_between():
    sql = (
        "SELECT o.order_id, SUM(o.amount) AS t FROM orders o "
        "WHERE o.order_id IN ('A1001', 'A1004') AND o.amount BETWEEN 50 AND 150 "
        "GROUP BY o.order_id"
    )
    data = run_query(sql).json()
    g = group_by_key(data)
    assert set(g) == {("A1001",)}
    assert g[("A1001",)]["total"] == 100.0


def test_evidence_truncation():
    orders = [{"order_id": "X", "amount": 1} for _ in range(100)]
    payments = [{"pay_id": f"P{i}", "order_id": "X"} for i in range(100)]
    r = client.post("/api/upload", json={"orders": orders, "payments": payments, "refunds": []})
    assert r.status_code == 200, r.text
    sql = (
        "SELECT o.order_id, SUM(o.amount) AS t FROM orders o "
        "INNER JOIN payments p ON o.order_id = p.order_id GROUP BY o.order_id"
    )
    data = run_query(sql).json()
    g = group_by_key(data)[("X",)]
    assert g["total"] == 10000
    assert len(g["orders"]) == 100
    assert all(o["count"] == 100 and o["duplicated"] for o in g["orders"])
    assert abs(sum(o["contribution"] for o in g["orders"]) - 10000) < 1e-6
    assert data["evidence"]["truncated"] is True
    assert len(data["evidence"]["rows"]) == data["evidence"]["limit"]


def test_execution_failure_reports_error(monkeypatch):
    def boom(ds, sql, params=()):
        raise sqlite3.Error("模拟执行失败")

    monkeypatch.setattr("app.storage.execute", boom)
    r = run_query(LEFT_SQL)
    assert r.status_code == 400
    assert "执行失败" in r.json()["detail"]
    assert "groups" not in r.json()
