"""DB-001 (indirect), DB-003, CMP-001, MEM-001, MEM-002, EXT-001: positives at ``# expect`` lines, negatives silent."""

from __future__ import annotations

import textwrap

import pytest

from scaletrace.pipeline import analyze_repository
from tests.conftest import marked_lines

CASES = {
    "DB-001": {
        "positive": {
            "helper_query_in_loop": """
                def load(db, uid):
                    return db.query(Order).filter(Order.user_id == uid).all()

                def report(db, users):
                    return [load(db, u.id) for u in users]  # expect
            """,
            "two_level_helper": """
                def fetch(db, uid):
                    return db.get(User, uid)

                def middle(db, uid):
                    return fetch(db, uid)

                def outer(db, ids):
                    for i in ids:
                        middle(db, i)  # expect
            """,
            "method_via_self": """
                class Repo:
                    def one(self, db, i):
                        return db.get(User, i)

                    def many(self, db, ids):
                        for i in ids:
                            self.one(db, i)  # expect
            """,
        },
        "negative": {
            "helper_without_db": """
                def fmt(x):
                    return str(x)

                def run(items):
                    return [fmt(i) for i in items]
            """,
            "helper_called_once": """
                def load(db):
                    return db.query(User).all()

                def run(db):
                    users = load(db)
                    return [u.name for u in users]
            """,
        },
    },
    "DB-003": {
        "positive": {
            "lazy_relationship_in_loop": """
                class User(Base):
                    orders = relationship("Order")

                def counts(db):
                    users = db.query(User).all()
                    return [len(u.orders) for u in users]  # expect
            """,
            "backref_in_for_loop": """
                class Author(Base):
                    books = relationship("Book", backref="author")

                def names(db):
                    out = []
                    for book in db.query(Book).all():
                        out.append(book.author.name)  # expect
                    return out
            """,
        },
        "negative": {
            "eager_option": """
                class User(Base):
                    orders = relationship("Order")

                def counts(db):
                    users = db.query(User).options(selectinload(User.orders)).all()
                    return [len(u.orders) for u in users]
            """,
            "eager_lazy_setting": """
                class User(Base):
                    orders = relationship("Order", lazy="selectin")

                def counts(db):
                    return [len(u.orders) for u in db.query(User).all()]
            """,
            "not_from_query": """
                class User(Base):
                    orders = relationship("Order")

                def counts(payload):
                    return [len(u.orders) for u in payload]
            """,
        },
    },
    "CMP-001": {
        "positive": {
            "regex_compile_in_loop": """
                import re

                def find(texts, q):
                    return [re.compile(q).search(t) for t in texts]  # expect
            """,
            "sorted_in_loop": """
                def ranks(items):
                    for item in items:
                        order = sorted(items)  # expect
            """,
        },
        "negative": {
            "compiled_once": """
                import re

                def find(texts, q):
                    pattern = re.compile(q)
                    return [pattern.search(t) for t in texts]
            """,
            "bounded_loop": """
                import json

                def dump():
                    for key in ("a", "b"):
                        json.dumps(key)
            """,
        },
    },
    "MEM-001": {
        "positive": {
            "all_without_limit": """
                def rows(db):
                    return db.query(User).all()  # expect
            """,
            "fetchall": """
                def rows(conn):
                    return conn.execute("SELECT * FROM events").fetchall()  # expect
            """,
            "list_of_query": """
                def rows(session):
                    return list(session.scalars(select(User)))  # expect
            """,
            "upload_read": """
                from fastapi import UploadFile

                async def upload(file: UploadFile):
                    data = await file.read()  # expect
            """,
        },
        "negative": {
            "limited": """
                def rows(db):
                    return db.query(User).limit(50).all()
            """,
            "limit_inside_select": """
                def rows(session):
                    return session.execute(select(User).limit(10)).scalars().all()
            """,
            "sql_limit": """
                def rows(conn):
                    return conn.execute("SELECT * FROM events LIMIT 100").fetchall()
            """,
            "non_db_all": """
                def check(flags):
                    return flags.all()
            """,
        },
    },
    "MEM-002": {
        "positive": {
            "module_list_append": """
                SEEN = []

                def track(item):
                    SEEN.append(item)  # expect
            """,
            "module_dict_item_assignment": """
                CACHE = {}

                def remember(key, value):
                    CACHE[key] = value  # expect
            """,
            "class_level_set": """
                class Registry:
                    names = set()

                    def register(self, name):
                        self.names.add(name)  # expect
            """,
        },
        "negative": {
            "local_list": """
                def build(items):
                    out = []
                    for i in items:
                        out.append(i)
                    return out
            """,
            "bounded_by_clear": """
                BUFFER = []

                def push(x):
                    BUFFER.append(x)

                def flush():
                    BUFFER.clear()
            """,
            "bounded_by_len_check": """
                CACHE = {}

                def remember(k, v):
                    if len(CACHE) > 1000:
                        return
                    CACHE[k] = v
            """,
            "deque_maxlen": """
                from collections import deque

                RECENT = deque(maxlen=100)

                def note(x):
                    RECENT.append(x)
            """,
            "instance_attribute": """
                class Box:
                    items = []

                    def __init__(self):
                        self.items = []

                    def add(self, x):
                        self.items.append(x)
            """,
        },
    },
    "EXT-001": {
        "positive": {
            "requests_in_loop": """
                import requests

                def fetch(ids):
                    for i in ids:
                        requests.get(f"https://api.example.com/u/{i}", timeout=5)  # expect
            """,
            "httpx_client_in_loop": """
                import httpx

                async def fetch(ids):
                    async with httpx.AsyncClient() as client:
                        for i in ids:
                            await client.get(f"https://api.example.com/{i}")  # expect
            """,
            "imported_function": """
                from requests import post

                def send(rows):
                    for row in rows:
                        post("https://hooks.example.com", json=row)  # expect
            """,
        },
        "negative": {
            "single_call": """
                import requests

                def fetch():
                    return requests.get("https://api.example.com", timeout=5)
            """,
            "unknown_client": """
                def fetch(client, ids):
                    for i in ids:
                        client.get(i)
            """,
        },
    },
}

PARAMS = [
    (rule, polarity, name)
    for rule, groups in CASES.items()
    for polarity, cases in groups.items()
    for name in cases
]


@pytest.mark.parametrize("rule,polarity,name", PARAMS, ids=[f"{r}-{p}-{n}" for r, p, n in PARAMS])
def test_rule_cases(analyze_snippet, rule, polarity, name):
    report, text = analyze_snippet(CASES[rule][polarity][name], rules=(rule,))
    lines = [f.line for f in report.findings]
    if polarity == "positive":
        assert lines == marked_lines(text)
    else:
        assert lines == []


def test_indirect_finding_records_call_chain(analyze_snippet):
    report, _ = analyze_snippet(CASES["DB-001"]["positive"]["two_level_helper"])
    [finding] = report.findings
    details = finding.static_evidence[0].details
    assert details["access"] == "indirect"
    assert details["call_chain"] == ["app:middle", "app:fetch"]


def test_cross_module_resolution_and_endpoints(tmp_path):
    files = {
        "app/__init__.py": "",
        "app/repo.py": "def orders_for(db, uid):\n    return db.query(Order).filter(Order.uid == uid).all()\n",
        "app/routes.py": textwrap.dedent(
            """
            from fastapi import APIRouter
            from .repo import orders_for
            from . import repo

            router = APIRouter(prefix="/users")

            @router.get("/report")
            def report(db, users):
                return [orders_for(db, u.id) for u in users]

            @router.get("/report2")
            def report2(db, users):
                for u in users:
                    repo.orders_for(db, u.id)
            """
        ),
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    report = analyze_repository(tmp_path, rule_ids=["DB-001", "MEM-001"])
    db001 = [f for f in report.findings if f.rule_id == "DB-001"]
    assert {(f.function, f.endpoint) for f in db001} == {("report", "GET /users/report"), ("report2", "GET /users/report2")}
    mem001 = [f for f in report.findings if f.rule_id == "MEM-001"]
    assert mem001[0].function == "orders_for"
    assert mem001[0].reachable_endpoints == ["GET /users/report", "GET /users/report2"]


def test_tests_directory_is_excluded(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def f(db, ids):\n    for i in ids:\n        db.get(U, i)\n", encoding="utf-8")
    (tmp_path / "test_top.py").write_text("def f(db, ids):\n    for i in ids:\n        db.get(U, i)\n", encoding="utf-8")
    assert analyze_repository(tmp_path).files_analyzed == 0
