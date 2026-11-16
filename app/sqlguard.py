"""只读聚合 SQL 的解析与白名单校验（基于 sqlglot）。

支持的 SQL 形态（orders 为主表）：
    SELECT <分组字段...>, SUM(<orders 的 amount>) [AS 别名]
    FROM orders <别名>
    [INNER|LEFT [OUTER] JOIN payments|refunds <别名> ON <条件>]...
    [WHERE <条件>]
    GROUP BY <字段...>

明确不支持：子查询、CTE、窗口函数、DISTINCT、HAVING、ORDER BY、LIMIT、
UNION、除 SUM 外的聚合、表达式/序号分组、逗号连接、RIGHT/FULL/CROSS JOIN、
USING、隐式别名。校验失败一律抛 QueryError，调用方不得展示推测结果。
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

MAIN_TABLE = "orders"
DIM_TABLES = ("payments", "refunds")
ALL_TABLES = (MAIN_TABLE, *DIM_TABLES)
AMOUNT_COLUMN = "amount"
ORDER_ID_COLUMN = "order_id"


class QueryError(Exception):
    """SQL 不被支持或无法执行。"""


@dataclass
class ResolvedColumn:
    table: str   # 实际表名 orders/payments/refunds
    column: str  # 实际字段名
    label: str   # 展示用，如 o.order_id


@dataclass
class ValidatedQuery:
    sql: str                     # 转译为 sqlite 方言的原查询（语义等价）
    select: exp.Select           # 通过校验的 AST（用于构建溯源查询）
    group_cols: list[ResolvedColumn]  # 按 GROUP BY 顺序
    sum_label: str
    group_pos: list[int]         # 各分组字段在原查询 SELECT 中的位置（与 group_cols 同序）
    sum_pos: int                 # SUM 在原查询 SELECT 中的位置
    table_alias: dict[str, str]  # 表名 -> 别名
    joined_tables: list[str]     # 实际 JOIN 的维表

    def alias_of(self, table: str) -> str:
        return self.table_alias[table]


# WHERE / ON 中允许的标量条件节点
_SCALAR_COND_NODES = (
    exp.And, exp.Or, exp.Not, exp.Paren,
    exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE,
    exp.Is, exp.In, exp.Like, exp.Between,
    exp.Column, exp.Literal, exp.Null, exp.Boolean, exp.Neg,
)

_FORBIDDEN_SELECT_ARGS = {
    "with": "不支持 CTE（WITH）",
    "having": "不支持 HAVING",
    "order": "不支持 ORDER BY",
    "limit": "不支持 LIMIT",
    "offset": "不支持 OFFSET",
    "distinct": "不支持 DISTINCT",
    "qualify": "不支持 QUALIFY",
    "into": "不支持 SELECT INTO",
    "locks": "不支持锁子句（FOR UPDATE 等）",
}


def _require_alias(table: exp.Table, what: str) -> str:
    alias = table.args.get("alias")
    if alias is None or not alias.name:
        raise QueryError(f"表 {what} 必须使用显式别名（如 {what} a）")
    return alias.name


def _check_condition(node: exp.Expression, where: str) -> None:
    for n in node.walk():
        if isinstance(n, exp.Identifier):
            continue  # 字段名的组成部分，随 Column 一并校验
        if not isinstance(n, _SCALAR_COND_NODES):
            raise QueryError(
                f"{where} 包含不支持的表达式 {n.sql(dialect='sqlite')!r}"
                "（仅允许字段、字面量与 =/!=/</>/<=/>=/AND/OR/NOT/IN/IS NULL/LIKE/BETWEEN）"
            )
        if isinstance(n, exp.In) and n.args.get("query") is not None:
            raise QueryError(f"{where} 不支持 IN 子查询")


def validate(sql_text: str, schemas: dict[str, list[str]]) -> ValidatedQuery:
    """校验 SQL 并返回 ValidatedQuery；任何不支持之处抛 QueryError。"""
    if not isinstance(sql_text, str) or not sql_text.strip():
        raise QueryError("SQL 为空")
    try:
        statements = [s for s in sqlglot.parse(sql_text) if s is not None]
    except Exception as e:
        raise QueryError(f"SQL 解析失败：{e}") from e
    if len(statements) != 1:
        raise QueryError("仅支持单条 SELECT 语句")
    stmt = statements[0]
    if not isinstance(stmt, exp.Select):
        raise QueryError("仅支持只读 SELECT 查询（不支持写操作/DDL/其他语句）")

    for arg, msg in _FORBIDDEN_SELECT_ARGS.items():
        if stmt.args.get(arg) is not None:
            raise QueryError(msg)

    for node in stmt.walk():
        if isinstance(node, exp.Subquery):
            raise QueryError("不支持子查询")
        if isinstance(node, exp.Window):
            raise QueryError("不支持窗口函数")
        if isinstance(node, exp.Distinct):
            raise QueryError("不支持 DISTINCT")
        if isinstance(node, exp.CTE):
            raise QueryError("不支持 CTE（WITH）")

    # ---- FROM：orders 主表 + 显式别名 ----
    from_ = stmt.args.get("from_")
    if from_ is None or not isinstance(from_.this, exp.Table):
        raise QueryError("FROM 必须是 orders 表（不支持子查询/表函数/空 FROM）")
    if from_.args.get("expressions"):
        raise QueryError("不支持逗号连接，请使用显式 INNER/LEFT JOIN ... ON")
    main = from_.this
    if main.args.get("db") or main.args.get("catalog"):
        raise QueryError("表名不允许带库名限定")
    if main.name.lower() != MAIN_TABLE:
        raise QueryError(f"主表必须是 orders，当前是 {main.name!r}")
    main_alias = _require_alias(main, MAIN_TABLE)

    table_alias: dict[str, str] = {MAIN_TABLE: main_alias}
    alias_owner: dict[str, str] = {}

    def register_alias(alias: str, table: str) -> None:
        low = alias.lower()
        if low in ALL_TABLES:
            raise QueryError(f"别名 {alias!r} 与表名冲突，请更换")
        if low in alias_owner:
            raise QueryError(f"别名 {alias!r} 重复")
        alias_owner[low] = table

    register_alias(main_alias, MAIN_TABLE)

    # ---- JOIN：仅 INNER/LEFT，目标仅 payments/refunds，各一次，必须 ON ----
    joined: list[str] = []
    for join in stmt.args.get("joins") or []:
        jt = join.this
        if not isinstance(jt, exp.Table):
            raise QueryError("JOIN 目标必须是 payments 或 refunds 表")
        side = (join.side or "").upper()
        kind = (join.kind or "").upper()
        if not ((side == "" and kind in ("", "INNER")) or (side == "LEFT" and kind in ("", "OUTER"))):
            desc = (side + " " + kind).strip() or "CROSS/隐式"
            raise QueryError(f"仅支持 INNER/LEFT JOIN，当前为 {desc} JOIN")
        if jt.args.get("db") or jt.args.get("catalog"):
            raise QueryError("表名不允许带库名限定")
        tname = jt.name.lower()
        if tname == MAIN_TABLE:
            raise QueryError("主表 orders 只能出现一次，不能再次 JOIN")
        if tname not in DIM_TABLES:
            raise QueryError(f"仅支持 JOIN payments/refunds，当前为 {jt.name!r}")
        if tname in joined:
            raise QueryError(f"表 {tname} 重复 JOIN（不支持同一维表多次关联）")
        alias = _require_alias(jt, tname)
        register_alias(alias, tname)
        if join.args.get("using"):
            raise QueryError("不支持 USING，请使用 ON 关联条件")
        on = join.args.get("on")
        if on is None:
            raise QueryError("JOIN 必须带 ON 条件（不支持笛卡尔积/逗号连接）")
        _check_condition(on, f"JOIN {tname} 的 ON 条件")
        joined.append(tname)
        table_alias[tname] = alias

    # ---- WHERE ----
    where = stmt.args.get("where")
    if where is not None:
        _check_condition(where.this, "WHERE")

    # ---- 字段解析（存在性 / 歧义）----
    col_maps = {t: {c.lower(): c for c in schemas[t]} for t in ALL_TABLES}

    def resolve(col: exp.Column) -> ResolvedColumn:
        name = col.name
        qualifier = col.table or ""
        if qualifier:
            t = alias_owner.get(qualifier.lower())
            if t is None:
                raise QueryError(f"未知表别名 {qualifier!r}（字段 {col.sql(dialect='sqlite')}）")
            actual = col_maps[t].get(name.lower())
            if actual is None:
                raise QueryError(f"表 {t} 不存在字段 {name!r}")
            return ResolvedColumn(t, actual, f"{qualifier}.{actual}")
        candidates = [t for t in table_alias if name.lower() in col_maps[t]]
        if not candidates:
            raise QueryError(f"未知字段 {name!r}")
        if len(candidates) > 1:
            raise QueryError(f"字段 {name!r} 存在歧义（多个表均有此字段），请加表别名限定")
        t = candidates[0]
        actual = col_maps[t][name.lower()]
        return ResolvedColumn(t, actual, f"{table_alias[t]}.{actual}")

    resolved_by_id = {
        id(n): resolve(n) for n in stmt.walk() if isinstance(n, exp.Column)
    }

    # ---- SELECT：仅分组字段 + 一个 SUM(orders.amount) ----
    select_col_items: list[tuple[int, tuple[str, str], str]] = []  # (位置, key, label)
    sum_label = ""
    sum_pos = -1
    sum_count = 0
    for i, item in enumerate(stmt.expressions):
        if item.find(exp.Star) is not None:
            raise QueryError("不支持 SELECT *，请显式列出分组字段与 SUM")
        inner = item.this if isinstance(item, exp.Alias) else item
        if isinstance(inner, exp.Column):
            rc = resolved_by_id[id(inner)]
            select_col_items.append((i, (rc.table, rc.column.lower()), rc.label))
        elif isinstance(inner, exp.Sum):
            sum_count += 1
            if sum_count > 1:
                raise QueryError("仅允许一个 SUM 聚合")
            arg = inner.this
            if not isinstance(arg, exp.Column):
                raise QueryError("SUM 的参数必须是 orders 的 amount 字段")
            rc = resolved_by_id[id(arg)]
            if not (rc.table == MAIN_TABLE and rc.column.lower() == AMOUNT_COLUMN):
                raise QueryError(f"SUM 仅支持 orders 的金额字段 {AMOUNT_COLUMN}，当前为 {rc.label}")
            sum_pos = i
            sum_label = item.alias_or_name or f"SUM({rc.label})"
        else:
            raise QueryError(
                f"SELECT 第 {i + 1} 项不支持：{item.sql(dialect='sqlite')}"
                "（仅允许分组字段与 SUM(orders.amount)）"
            )
    if sum_count == 0:
        raise QueryError(f"SELECT 必须包含一个 SUM(orders.{AMOUNT_COLUMN}) 聚合")

    # ---- GROUP BY：仅字段，且与 SELECT 字段一一对应 ----
    group = stmt.args.get("group")
    if group is None or not group.expressions:
        raise QueryError("必须使用 GROUP BY 按字段分组")
    group_cols: list[ResolvedColumn] = []
    for g in group.expressions:
        if not isinstance(g, exp.Column):
            raise QueryError(f"GROUP BY 仅支持字段，不支持表达式/序号：{g.sql(dialect='sqlite')}")
        group_cols.append(resolved_by_id[id(g)])
    group_keys = [(c.table, c.column.lower()) for c in group_cols]

    group_pos: list[int] = []
    for key, col in zip(group_keys, group_cols):
        pos = next((i for i, k, _ in select_col_items if k == key), None)
        if pos is None:
            raise QueryError(f"GROUP BY 字段 {col.label} 必须同时出现在 SELECT 中")
        group_pos.append(pos)
    for _, key, label in select_col_items:
        if key not in group_keys:
            raise QueryError(f"SELECT 字段 {label} 必须出现在 GROUP BY 中")

    return ValidatedQuery(
        sql=stmt.sql(dialect="sqlite"),
        select=stmt,
        group_cols=group_cols,
        sum_label=sum_label,
        group_pos=group_pos,
        sum_pos=sum_pos,
        table_alias=table_alias,
        joined_tables=joined,
    )
