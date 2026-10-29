"""DB-001: per-batch work in paged ``while`` loops (docs/error_analysis.md, category DB-1).

Motivating dev-split code (good-c014 ``_anonymise_pending`` and ``clear_expired_idempotency_keys``): a ``while``
loop fetches one page with ``.limit(batch_size)`` and stops when a page comes back empty or short. Such a loop
issues one query per page, not one per row, so its direct body is not the per-item N+1 pattern DB-001 targets.
Per-item work nested inside the page (an inner ``for`` loop) must still be reported.
"""

from __future__ import annotations

import pytest

from tests.conftest import marked_lines

CASES = {
    "negative": {
        "keyset_page_with_break_on_empty": """
            def anonymise(db, batch_size):
                last_id = 0
                while True:
                    accounts = db.scalars(select(User).where(User.id > last_id).limit(batch_size)).all()
                    if not accounts:
                        break
                    last_id = accounts[-1].id
                    db.execute(update(User).where(User.id.in_([a.id for a in accounts])).values(deleted=True))
                    db.commit()
                    if len(accounts) < batch_size:
                        break
        """,
        "two_step_page_with_break": """
            def clear(db, batch_size=500):
                while True:
                    ids = db.execute(select(Order.id).where(Order.key.isnot(None)).limit(batch_size)).scalars().all()
                    if not ids:
                        break
                    db.execute(update(Order).where(Order.id.in_(ids)).values(key=None))
                    db.commit()
        """,
        "page_variable_in_loop_condition": """
            def drain(db):
                rows = db.query(Job).limit(100).all()
                while rows:
                    db.query(Job).filter(Job.id.in_([r.id for r in rows])).delete()
                    rows = db.query(Job).limit(100).all()
        """,
    },
    "positive": {
        "per_item_query_inside_page_still_reported": """
            def anonymise(db, batch_size):
                while True:
                    accounts = db.scalars(select(User).limit(batch_size)).all()  # one query per page: not reported
                    if not accounts:
                        break
                    for account in accounts:
                        db.get(Profile, account.id)  # expect
        """,
        "limit_one_is_per_item": """
            def pop_all(db):
                while True:
                    row = db.query(Task).limit(1).all()  # expect
                    if not row:
                        break
                    db.query(Task).filter(Task.id == row[0].id).delete()  # expect
        """,
        "limit_without_stop_condition": """
            def poll(db):
                while True:
                    jobs = db.query(Job).limit(50).all()  # expect
                    process(jobs)
        """,
        "break_on_other_variable": """
            def loop(db, items):
                while True:
                    page = db.query(Job).limit(50).all()  # expect
                    if not items:
                        break
        """,
        "for_loop_is_not_a_paged_loop": """
            def each(db, ids):
                for i in ids:
                    db.query(Job).filter(Job.id == i).limit(10).all()  # expect
        """,
    },
}

PARAMS = [(polarity, name) for polarity, cases in CASES.items() for name in cases]


@pytest.mark.parametrize("polarity,name", PARAMS, ids=[f"{p}-{n}" for p, n in PARAMS])
def test_paged_loops(analyze_snippet, polarity, name):
    report, text = analyze_snippet(CASES[polarity][name], rules=("DB-001",))
    lines = sorted(f.line for f in report.findings)
    assert lines == (marked_lines(text) if polarity == "positive" else [])


def test_paged_loop_reason_is_recorded():
    import ast

    from scaletrace.analyzer.loops import LoopWalker

    tree = ast.parse(
        "def f(db, n):\n"
        "    while True:\n"
        "        rows = db.query(X).limit(n).all()\n"
        "        if not rows:\n"
        "            break\n"
    )
    loops = {loop for _, ls in LoopWalker(10).walk(tree.body[0]) for loop in ls}
    [loop] = loops
    assert loop.bounded and "paged loop" in loop.bound_reason and "`rows`" in loop.bound_reason
