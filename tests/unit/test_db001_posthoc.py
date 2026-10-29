"""DB-001 fixes informed by inspecting the LOCKED TEST SPLIT (post-hoc; see docs/error_analysis.md section 8).

Both patterns were first seen in pre-lock review notes of test-split apps (good-c025, good-c021). The tests use
synthetic code only. Any test-split evaluation after these changes is post-hoc, not an independent evaluation.

1. A call to a project ``async def`` helper that is discarded without ``await`` (a bare expression statement)
   only creates a coroutine object; the helper's body, and so its query, never runs. Calls that are awaited,
   scheduled (``create_task``, ``gather``, ``ensure_future``) or kept for later are still reported, as are
   decorated helpers, whose call semantics are not visible.
2. A loop over a LOCAL name bound once to a small literal list/tuple/set, which is never re-bound, mutated,
   aliased or passed to another function, iterates a fixed number of times, like a loop over the literal itself.
"""

from __future__ import annotations

import pytest

from tests.conftest import marked_lines

HELPER = """
    async def release(db, item_id):
        await db.execute(update(Stock).where(Stock.id == item_id).values(reserved=0))
"""

CASES = {
    # --- 1. async helpers --------------------------------------------------------------------------------------
    "unawaited_bare_call": (HELPER + """
    async def cancel(db, items):
        for item in items:
            release(db, item.id)
    """, False),
    "unawaited_method_call": ("""
    class Inventory:
        async def release(self, db, item_id):
            await db.execute(update(Stock).where(Stock.id == item_id))

        async def cancel(self, db, items):
            for item in items:
                self.release(db, item.id)
    """, False),
    "awaited_call": (HELPER + """
    async def cancel(db, items):
        for item in items:
            await release(db, item.id)  # expect
    """, True),
    "awaited_inside_expression": (HELPER + """
    async def cancel(db, items):
        for item in items:
            done = await release(db, item.id) or True  # expect
    """, True),
    "create_task_scheduled": (HELPER + """
    async def cancel(db, items):
        for item in items:
            asyncio.create_task(release(db, item.id))  # expect
    """, True),
    "collected_then_gathered": (HELPER + """
    async def cancel(db, items):
        tasks = []
        for item in items:
            tasks.append(release(db, item.id))  # expect
        await asyncio.gather(*tasks)
    """, True),
    "gather_comprehension": (HELPER + """
    async def cancel(db, items):
        await asyncio.gather(*[release(db, item.id) for item in items])  # expect
    """, True),
    "assigned_coroutine": (HELPER + """
    async def cancel(db, items):
        for item in items:
            pending = release(db, item.id)  # expect
            await pending
    """, True),
    "sync_helper_bare_call": ("""
    def release(db, item_id):
        db.execute(update(Stock).where(Stock.id == item_id))

    def cancel(db, items):
        for item in items:
            release(db, item.id)  # expect
    """, True),
    "decorated_async_helper": ("""
    @run_in_background
    async def release(db, item_id):
        await db.execute(update(Stock).where(Stock.id == item_id))

    async def cancel(db, items):
        for item in items:
            release(db, item.id)  # expect
    """, True),
    "unawaited_direct_query_still_reported": ("""
    async def cancel(db, items):
        for item in items:
            db.execute(update(Stock).where(Stock.id == item.id))  # expect
    """, True),

    # --- 2. local literal collections ---------------------------------------------------------------------------
    "local_literal_list": ("""
    def dashboard(db, user):
        categories = ["food", "rent", "travel"]
        for category in categories:
            db.query(Expense).filter(Expense.user_id == user.id, Expense.category == category).count()
    """, False),
    "local_literal_tuple_annotated": ("""
    def dashboard(db, user):
        months: tuple[int, ...] = (1, 2, 3, 4, 5, 6)
        return [db.query(Expense).filter(Expense.month == m).count() for m in months]
    """, False),
    "local_literal_used_by_len": ("""
    def dashboard(db):
        kinds = ("a", "b")
        print(len(kinds))
        for kind in kinds:
            db.query(X).filter(X.kind == kind).count()
    """, False),
    "parameter_collection": ("""
    def dashboard(db, categories):
        for category in categories:
            db.query(Expense).filter(Expense.category == category).count()  # expect
    """, True),
    "local_list_from_query": ("""
    def dashboard(db):
        categories = db.query(Category).limit(5).all()
        for category in categories:
            db.query(Expense).filter(Expense.category_id == category.id).count()  # expect
    """, True),
    "local_literal_then_appended": ("""
    def dashboard(db, extra):
        categories = ["food", "rent"]
        categories.append(extra)
        for category in categories:
            db.query(Expense).filter(Expense.category == category).count()  # expect
    """, True),
    "local_literal_then_extended_in_loop": ("""
    def dashboard(db, extras):
        categories = ["food"]
        for e in extras:
            categories += [e]
        for category in categories:
            db.query(Expense).filter(Expense.category == category).count()  # expect
    """, True),
    "local_literal_rebound": ("""
    def dashboard(db, user):
        categories = ["food", "rent"]
        if user.custom:
            categories = user.categories
        for category in categories:
            db.query(Expense).filter(Expense.category == category).count()  # expect
    """, True),
    "local_literal_passed_to_function": ("""
    def dashboard(db, user):
        categories = ["food", "rent"]
        add_user_categories(categories, user)
        for category in categories:
            db.query(Expense).filter(Expense.category == category).count()  # expect
    """, True),
    "local_literal_aliased": ("""
    def dashboard(db, extra):
        categories = ["food", "rent"]
        same = categories
        same.append(extra)
        for category in categories:
            db.query(Expense).filter(Expense.category == category).count()  # expect
    """, True),
    "local_literal_too_long": ("""
    def dashboard(db):
        ids = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
        for i in ids:
            db.get(Item, i)  # expect
    """, True),
    "local_literal_with_starred": ("""
    def dashboard(db, more):
        kinds = ["a", *more]
        for kind in kinds:
            db.query(X).filter(X.kind == kind).count()  # expect
    """, True),
    "per_item_query_inside_bounded_outer_loop": ("""
    def dashboard(db, user):
        months = [1, 2, 3]
        for month in months:
            for expense in user.expenses:
                db.get(Receipt, expense.receipt_id)  # expect
    """, True),
}


@pytest.mark.parametrize("name", list(CASES))
def test_db001_posthoc_cases(analyze_snippet, name):
    source, reported = CASES[name]
    report, text = analyze_snippet(source, rules=("DB-001",))
    lines = sorted(f.line for f in report.findings)
    assert lines == (marked_lines(text) if reported else []), name


def test_local_literal_bound_reason_is_recorded():
    import ast

    from scaletrace.analyzer.loops import LoopWalker

    tree = ast.parse("def f(db):\n    months = [1, 2, 3]\n    for m in months:\n        db.get(X, m)\n")
    walker = LoopWalker(10)
    walker.register_module(tree)
    [loop] = {loop for _, ls in walker.walk(tree.body[0]) for loop in ls}
    assert loop.bounded and "local constant `months`" in loop.bound_reason
