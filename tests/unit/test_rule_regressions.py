"""Regression tests for the 29 October 2026 rule fixes (see docs/rule_changes.md).

Each case is a minimal reproduction of a source pattern seen in the DEV split of the benchmark
(never the locked test split), plus nearby safe patterns that must stay silent.

1. MEM-001 two-step reads: ``result = await db.execute(stmt)`` then ``result.scalars().all()``
   (good-c001 ``BaseRepository.get_where``; good-c032 ``get_tasks`` is the bounded counterpart).
2. MEM-001 bound on a statement variable: ``stmt = select(...).limit(n)`` executed later
   (good-c001 ``count_and_get_many``).
3. DB-001 loops over a module-level constant literal collection
   (good-c014 migration ``find_violations`` iterating ``CONSTRAINTS``).
"""

from __future__ import annotations

import pytest

from tests.conftest import marked_lines

CASES = {
    "MEM-001": {
        "positive": {
            "async_two_step_scalars_all": """
                async def get_where(session, filters):
                    statement = select(User).where(*filters)
                    result = await session.execute(statement)
                    return result.scalars().all()  # expect
            """,
            "async_two_step_inline_select": """
                async def list_users(db):
                    result = await db.execute(select(User))
                    users = result.scalars().all()  # expect
                    return users
            """,
            "sync_two_step_result_all": """
                def rows(session):
                    result = session.execute(select(Order).order_by(Order.id))
                    return result.all()  # expect
            """,
            "two_step_unique_scalars_all": """
                async def rows(session):
                    result = await session.execute(select(Order))
                    return result.unique().scalars().all()  # expect
            """,
            "two_step_fetchall_sql": """
                def events(conn):
                    cursor = conn.execute("SELECT * FROM events")
                    return cursor.fetchall()  # expect
            """,
            "statement_variable_without_limit_inline": """
                def rows(db):
                    stmt = select(User).where(User.active)
                    return db.execute(stmt).scalars().all()  # expect
            """,
            "returning_without_insert_is_not_a_bound": """
                def archive(db):
                    result = db.execute(update(Order).where(Order.done).values(archived=True).returning(Order.id))
                    return result.scalars().all()  # expect
            """,
            "limit_attribute_is_not_a_limit_call": """
                def rows(db, params):
                    stmt = select(User).offset(params.limit)
                    result = db.execute(stmt)
                    return result.scalars().all()  # expect
            """,
        },
        "negative": {
            "two_step_limit_inside_select": """
                async def get_tasks(db, skip: int = 0, limit: int = 10):
                    result = await db.execute(select(Task).offset(skip).limit(limit))
                    return result.scalars().all()
            """,
            "limit_on_statement_variable": """
                async def page(session, pagination):
                    select_statement = select(User).order_by(User.id).limit(pagination.limit)
                    select_statement = select_statement.where(User.active)
                    result = await session.execute(statement=select_statement)
                    return result.scalars().all()
            """,
            "limit_applied_later_to_statement": """
                def page(db, n):
                    stmt = select(User)
                    stmt = stmt.limit(n)
                    return db.execute(stmt).scalars().all()
            """,
            "two_step_first": """
                async def one(db, uid):
                    result = await db.execute(select(User).where(User.id == uid))
                    return result.scalars().first()
            """,
            "two_step_non_db_result": """
                def check(client):
                    result = client.get("/flags")
                    return result.all()
            """,
            "sql_limit_in_variable": """
                def rows(conn):
                    sql = "SELECT * FROM events ORDER BY id LIMIT 100"
                    return conn.execute(sql).fetchall()
            """,
            "insert_returning_two_step": """
                async def create_many(session, model, data):
                    insert_statement = postgresql_insert(model).values(list(data)).returning(model)
                    statement = select(model).from_statement(insert_statement)
                    result = await session.execute(statement=statement)
                    return result.scalars().all()
            """,
            "insert_returning_inline": """
                def create_rows(db, rows):
                    return db.execute(insert(User).values(rows).returning(User)).scalars().all()
            """,
            "result_reassigned_to_non_db": """
                def rows(db, items):
                    result = db.execute(select(User))
                    result = Collection(items)
                    return result.all()
            """,
        },
    },
    "DB-001": {
        "positive": {
            "module_list_that_is_mutated": """
                TABLES = ["users", "orders"]

                def register(name):
                    TABLES.append(name)

                def count_all(conn):
                    for table in TABLES:
                        conn.execute(text(f"SELECT count(*) FROM {table}"))  # expect
            """,
            "module_name_shadowed_by_parameter": """
                CONSTRAINTS = [("a", 1), ("b", 2)]

                def check(conn, CONSTRAINTS):
                    for table, value in CONSTRAINTS:
                        conn.execute(text(f"SELECT {value} FROM {table}"))  # expect
            """,
            "module_name_reassigned": """
                NAMES = ("a", "b")
                NAMES = load_names()

                def check(conn):
                    for name in NAMES:
                        conn.execute(text(name))  # expect
            """,
            "module_constant_too_long": """
                IDS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]

                def load(db):
                    for i in IDS:
                        db.get(User, i)  # expect
            """,
        },
        "negative": {
            "module_constant_literal": """
                CONSTRAINTS = [
                    ("products", "ck_price", "price >= 0"),
                    ("products", "ck_stock", "stock >= 0"),
                ]

                def find_violations(connection):
                    violations = []
                    for table, name, condition in CONSTRAINTS:
                        count = connection.execute(text(f"SELECT count(*) FROM {table} WHERE NOT ({condition})")).scalar()
                        if count:
                            violations.append(name)
                    return violations
            """,
            "module_constant_tuple_annotated": """
                KINDS: tuple[str, ...] = ("a", "b", "c")

                def load(db):
                    return [db.query(Kind).filter(Kind.name == k).first() for k in KINDS]
            """,
        },
    },
}

PARAMS = [(rule, polarity, name) for rule, groups in CASES.items() for polarity, cases in groups.items() for name in cases]


@pytest.mark.parametrize("rule,polarity,name", PARAMS, ids=[f"{r}-{p}-{n}" for r, p, n in PARAMS])
def test_regression_cases(analyze_snippet, rule, polarity, name):
    report, text = analyze_snippet(CASES[rule][polarity][name], rules=(rule,))
    lines = [f.line for f in report.findings]
    if polarity == "positive":
        assert lines == marked_lines(text)
    else:
        assert lines == []


def test_two_step_evidence_names_the_result_variable(analyze_snippet):
    report, _ = analyze_snippet(CASES["MEM-001"]["positive"]["async_two_step_scalars_all"], rules=("MEM-001",))
    [finding] = report.findings
    details = finding.static_evidence[0].details
    assert details["receiver"] == "result"
    assert details["materialized_from"] == "session.execute(statement)"
