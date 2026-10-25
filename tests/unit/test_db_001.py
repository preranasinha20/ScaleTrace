"""DB-001 detector: positive cases must be found at the marked line, negatives must stay silent.

Lines marked ``# expect`` are where a finding is expected; every other line must have none.
"""

from __future__ import annotations

import pytest

from tests.conftest import marked_lines

POSITIVE_CASES = {
    "annotated_session_for_loop": """
        from sqlalchemy.orm import Session

        def summaries(db: Session, users):
            out = []
            for user in users:
                out.append(db.query(Order).filter(Order.user_id == user.id).all())  # expect
            return out
    """,
    "fastapi_depends_without_annotation": """
        from fastapi import Depends

        @app.get("/items")
        def items(ids: list[int], s=Depends(get_session)):
            for item_id in ids:
                s.get(Item, item_id)  # expect
    """,
    "annotated_dependency": """
        from typing import Annotated
        from fastapi import Depends

        def items(ids: list[int], store: Annotated[object, Depends(get_db)]):
            for item_id in ids:
                store.get(Item, item_id)  # expect
    """,
    "async_session_execute": """
        from sqlalchemy.ext.asyncio import AsyncSession

        async def load(session: AsyncSession, ids):
            for i in ids:
                await session.execute(select(Item).where(Item.id == i))  # expect
    """,
    "while_loop": """
        def drain(db, queue):
            while queue:
                db.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": queue.pop()})  # expect
    """,
    "while_condition_queries": """
        def drain(db):
            while db.scalar(select(func.count(Job.id))) > 0:  # expect
                pass
    """,
    "list_comprehension": """
        def fetch_all(db, ids):
            return [db.get(User, i) for i in ids]  # expect
    """,
    "second_generator_iterable": """
        def pairs(db, users):
            return [(u, o) for u in users for o in db.query(Order).filter(Order.user_id == u.id)]  # expect
    """,
    "factory_assigned_session": """
        def report(rows):
            with SessionLocal() as unit:
                for row in rows:
                    unit.merge(row)  # expect
    """,
    "engine_connect_handle": """
        def bulk(engine, rows):
            with engine.connect() as link:
                for row in rows:
                    link.execute(insert(T).values(**row))  # expect
    """,
    "raw_cursor": """
        import sqlite3

        def names(ids):
            con = sqlite3.connect("x.db")
            cur = con.cursor()
            for i in ids:
                cur.execute("SELECT name FROM t WHERE id = ?", (i,))  # expect
    """,
    "repository_self_attribute": """
        class UserRepository:
            def __init__(self, session):
                self.session = session

            def by_ids(self, ids):
                return [self.session.get(User, i) for i in ids]  # expect
    """,
    "nested_loops_reported_once": """
        def matrix(db, users, products):
            for user in users:
                for product in products:
                    db.query(Purchase).filter_by(user_id=user.id, product_id=product.id).first()  # expect
    """,
    "unbounded_inside_bounded": """
        def by_status(db, users):
            for status in ("new", "paid"):
                for user in users:
                    db.query(Order).filter_by(user_id=user.id, status=status).count()  # expect
    """,
    "large_constant_range": """
        def warm(db):
            for i in range(1000):
                db.get(User, i)  # expect
    """,
    "commit_per_iteration": """
        def save_all(db, items):
            for item in items:
                db.add(item)
                db.commit()  # expect
    """,
}

NEGATIVE_CASES = {
    "query_in_for_iterable_runs_once": """
        def names(db):
            return [u.name for u in db.query(User).all()]

        def loop(db):
            for user in db.query(User).all():
                print(user.name)
    """,
    "small_literal_loop": """
        def counts(db):
            for status in ("new", "paid", "shipped"):
                db.query(Order).filter_by(status=status).count()
            for i in range(3):
                db.get(User, i)
    """,
    "dict_get_is_not_db": """
        def lookup(cache, keys):
            return [cache.get(k) for k in keys]
    """,
    "requests_session_is_not_db": """
        import requests

        def fetch(urls):
            session = requests.Session()
            for url in urls:
                session.get(url, timeout=5)
    """,
    "requests_session_annotation_is_not_db": """
        import requests

        def fetch(session: requests.Session, urls):
            for url in urls:
                session.get(url, timeout=5)
    """,
    "add_is_not_a_round_trip": """
        def stage(db, items):
            for item in items:
                db.add(item)
            db.commit()
    """,
    "nested_def_body_not_run_in_loop": """
        def build(db, ids):
            handlers = []
            for i in ids:
                def handler():
                    return db.get(User, i)
                handlers.append(handler)
            return handlers
    """,
    "for_else_runs_once": """
        def find(db, users):
            for user in users:
                if user.active:
                    break
            else:
                db.query(User).first()
    """,
    "sessionmaker_is_a_factory_not_a_session": """
        SessionFactory = sessionmaker(bind=engine)

        def warm(names):
            for name in names:
                SessionFactory.configure(info={"name": name})
    """,
}


@pytest.mark.parametrize("name", sorted(POSITIVE_CASES))
def test_positive_cases_found_at_marked_line(analyze_snippet, name):
    report, text = analyze_snippet(POSITIVE_CASES[name])
    assert [f.line for f in report.findings] == marked_lines(text)
    assert all(f.rule_id == "DB-001" for f in report.findings)


@pytest.mark.parametrize("name", sorted(NEGATIVE_CASES))
def test_negative_cases_report_nothing(analyze_snippet, name):
    report, _ = analyze_snippet(NEGATIVE_CASES[name])
    assert report.findings == []


def test_finding_contains_contract_fields_and_evidence(analyze_snippet):
    report, text = analyze_snippet(
        """
        from fastapi import APIRouter, Depends
        from sqlalchemy.orm import Session

        router = APIRouter(prefix="/users")

        @router.get("/{user_id}/orders")
        def orders(user_id: int, db: Session = Depends(get_db)):
            result = []
            for order_id in ids_for(user_id):
                result.append(db.get(Order, order_id))  # expect
            return result
        """
    )
    [finding] = report.findings
    assert finding.line == marked_lines(text)[0]
    assert finding.file == "app.py"
    assert finding.function == "orders"
    assert finding.endpoint == "GET /users/{user_id}/orders"
    assert finding.severity.value == "high"
    assert finding.classification == "deterministic"
    assert finding.status.value == "static_candidate"
    assert finding.runtime_evidence == []
    assert finding.possible_false_positives  # copied from the rulebook
    assert finding.verification_steps

    [evidence] = finding.static_evidence
    assert evidence.pattern == "database call nested inside for or while loop"
    assert evidence.snippet == "db.get(Order, order_id)"
    assert evidence.details["handle_inference"] == "annotation"
    assert evidence.details["loop_variables_in_query"] == ["order_id"]
    assert evidence.details["innermost_loop"]["header"] == "for order_id in ids_for(user_id)"


def test_method_and_class_qualnames(analyze_snippet):
    report, _ = analyze_snippet(
        """
        class Service:
            def run(self, db, ids):
                for i in ids:
                    db.get(User, i)
        """
    )
    [finding] = report.findings
    assert finding.function == "Service.run"
    assert finding.endpoint is None


def test_name_heuristic_is_reported_as_such(analyze_snippet):
    report, _ = analyze_snippet(
        """
        def f(db, ids):
            for i in ids:
                db.get(User, i)
        """
    )
    assert report.findings[0].static_evidence[0].details["handle_inference"] == "name_heuristic"
