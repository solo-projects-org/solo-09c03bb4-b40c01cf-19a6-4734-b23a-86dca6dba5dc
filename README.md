# 结算订单金额溯源

结算数据工程师排查「支付 / 退款关联导致订单金额虚高」的溯源网页工具：
上传三表 JSON，输入以 orders 为主表的只读聚合 SQL，系统运行原查询，并逐组逐单
列出参与汇总的关联行组合、计入次数与贡献金额，标出被重复计入的订单。

技术栈：FastAPI + SQLite（内存库）+ sqlglot（SQL 解析 / 白名单校验）+ 原生前端。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 自测

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## 数据格式（三表 JSON）

```json
{
  "orders":   [{"order_id": "A1001", "amount": 100.0, "channel": "APP"}],
  "payments": [{"pay_id": "P9001", "order_id": "A1001"}],
  "refunds":  [{"refund_id": "R7001", "order_id": "A1001"}]
}
```

约束（上传时校验，失败即明确报错）：

- `orders`：每行 `order_id` 非空、`amount` 为非空数值；
- `payments` / `refunds`：行号 `pay_id` / `refund_id` 非空且表内唯一，`order_id` 非空；
- 三表合计 ≤ 500 行；允许额外标量字段（如 `orders.channel`，可用于分组）；
- 字段名限字母 / 数字 / 下划线，禁用 `rowid`、`__` 前缀等保留名。

页面内置「载入示例数据」与两条示例查询，可直接运行。

## SQL 支持范围

仅支持如下形态（所有表必须显式别名）：

```sql
SELECT <分组字段...>, SUM(<orders 的 amount>) [AS 别名]
FROM orders <别名>
[INNER|LEFT [OUTER] JOIN payments|refunds <别名> ON <条件>]...
[WHERE <条件>]
GROUP BY <字段...>
```

- JOIN：仅 INNER / LEFT，payments、refunds 各至多一次，必须带 ON；
- WHERE / ON：字段、字面量与 `= != < > <= >= AND OR NOT IN(字面量) IS [NOT] NULL LIKE BETWEEN`；
- GROUP BY 仅字段，且与 SELECT 中的非聚合字段一一对应；
- SELECT 仅分组字段 + 一个 `SUM(orders.amount)`。

明确不支持（解析即报错，不展示推测结果）：子查询、CTE、窗口函数、DISTINCT、
HAVING、ORDER BY、LIMIT、UNION、其他聚合函数、表达式 / 序号分组、
逗号连接、RIGHT / FULL / CROSS JOIN、USING、写操作与 DDL。

## 溯源原理（不改变查询语义）

1. **原查询**：按用户 SQL 原样（经 sqlglot 转译为 SQLite 方言）执行，得到各组实际汇总；
2. **贡献查询**：保持 FROM / JOIN / WHERE 不变，仅把分组细化为
   `分组字段 + orders.rowid`，得到每单 `计入次数 = COUNT(*)`、`贡献金额 = SUM(amount)`；
   同额订单按 rowid 区分，互不归并；
3. **证据查询**：同样保持 FROM / JOIN / WHERE 不变，去掉聚合逐行返回关联行组合
   （orders / payments / refunds 的 rowid），再按 rowid 回查原始行展示；
   LEFT JOIN 空行、NULL 过滤、重复匹配均由 SQLite 按原 SQL 语义自然得出；
4. **完整性校验**：每组内各订单贡献之和必须等于该组实际汇总，校验通过才展示；
   不一致或执行失败时返回错误，不展示推测归因。

行级证据超过 2000 条时截断并明确标记（汇总与贡献数值不受影响）。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 网页 |
| GET | `/api/sample` | 示例数据与示例查询 |
| POST | `/api/upload` | 上传三表 JSON，返回装载摘要 |
| GET | `/api/dataset` | 当前数据集摘要 |
| POST | `/api/query` | `{ "sql": "..." }`，返回分组汇总、逐单贡献与行级证据 |

错误统一返回 `{"detail": "..."}`（400 为用户输入问题，500 为内部校验失败）。

## 已知限制

- 单数据集：新上传覆盖旧数据（无多会话隔离）；
- 金额为 SQLite 数值求和，浮点金额可能存在尾差，校验容差 1e-6（相对）；
- 行级证据展示上限 2000 条，超出截断并提示。
