"""Parsing a source file into an AST plus the lookups detectors need."""

from __future__ import annotations

import ast
import importlib.util
import warnings
from dataclasses import dataclass, field

from scaletrace.analyzer.ingestion import SourceFile
from scaletrace.analyzer.scopes import FunctionScope, collect_scopes
from scaletrace.schemas import SkippedFile

SNIPPET_MAX_CHARS = 160


@dataclass
class ModuleContext:
    """A parsed module. Built from source text only; the module is never imported."""

    rel_path: str
    source: str
    tree: ast.Module
    scopes: list[FunctionScope] = field(default_factory=list)
    _parents: dict[ast.AST, ast.AST] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for parent in ast.walk(self.tree):
            for child in ast.iter_child_nodes(parent):
                self._parents[child] = parent
        self.scopes = collect_scopes(self.tree)

    def parent(self, node: ast.AST) -> ast.AST | None:
        return self._parents.get(node)

    def outermost_chain(self, node: ast.AST) -> ast.AST:
        """Climb a method chain such as ``db.query(X).filter(...).all()`` to its outermost call."""
        current = node
        while True:
            parent = self._parents.get(current)
            if isinstance(parent, ast.Attribute) and parent.value is current:
                current = parent
            elif isinstance(parent, ast.Call) and parent.func is current:
                current = parent
            elif isinstance(parent, ast.Await):
                current = parent
            else:
                return current

    def snippet(self, node: ast.AST) -> str:
        text = ast.get_source_segment(self.source, node) or ast.unparse(node)
        text = " ".join(text.split())
        if len(text) > SNIPPET_MAX_CHARS:
            text = text[: SNIPPET_MAX_CHARS - 3] + "..."
        return text


def parse_module(source_file: SourceFile) -> ModuleContext | SkippedFile:
    """Parse ``source_file``; return a SkippedFile with a reason instead of raising."""
    try:
        raw = source_file.path.read_bytes()
    except OSError as exc:
        return SkippedFile(file=source_file.rel_path, reason=f"unreadable: {exc.strerror}")
    try:
        source = importlib.util.decode_source(raw)
    except (UnicodeDecodeError, SyntaxError, LookupError) as exc:
        return SkippedFile(file=source_file.rel_path, reason=f"cannot decode source: {exc}")
    try:
        with warnings.catch_warnings():
            # Target code may contain e.g. invalid escape sequences; that is not our concern.
            warnings.simplefilter("ignore")
            tree = ast.parse(source, filename=source_file.rel_path)
    except SyntaxError as exc:
        return SkippedFile(file=source_file.rel_path, reason=f"syntax error at line {exc.lineno}: {exc.msg}")
    except ValueError as exc:  # e.g. null bytes
        return SkippedFile(file=source_file.rel_path, reason=f"cannot parse: {exc}")
    return ModuleContext(rel_path=source_file.rel_path, source=source, tree=tree)
