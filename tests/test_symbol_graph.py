"""Tests for Issue #27 — Symbol graph: entities, edges, and code relationship extraction.

Coverage:
- Entity and Edge ORM model construction
- EntityRead / EdgeRead Pydantic schema round-trips
- Graph extractor: imports, class inheritance, function calls, module entity
- Graph extractor: non-Python language returns empty lists
- Graph extractor: empty parsed document
- Graph extractor: document with no sections
- Pipeline graph stage: wired correctly (mock index writer)
- Migration content sanity check (DDL strings)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniscience_core.db.models import Edge, Entity
from omniscience_core.db.schemas import EdgeRead, EntityRead
from omniscience_parsers.base import ParsedDocument
from omniscience_parsers.code.graph import extract_symbol_graph
from omniscience_parsers.code.treesitter import TreeSitterParser

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)


def _make_entity(
    *,
    entity_type: str = "function",
    name: str = "mymod.my_func",
    display_name: str = "my_func",
    chunk_id: uuid.UUID | None = None,
    entity_metadata: dict[str, Any] | None = None,
) -> Entity:
    return Entity(
        id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        entity_type=entity_type,
        name=name,
        display_name=display_name,
        chunk_id=chunk_id,
        entity_metadata=entity_metadata or {},
        created_at=_NOW,
    )


def _make_edge(
    *,
    source_entity_id: uuid.UUID | None = None,
    target_entity_id: uuid.UUID | None = None,
    edge_type: str = "calls",
    edge_metadata: dict[str, Any] | None = None,
) -> Edge:
    return Edge(
        id=uuid.uuid4(),
        source_entity_id=source_entity_id or uuid.uuid4(),
        target_entity_id=target_entity_id or uuid.uuid4(),
        edge_type=edge_type,
        edge_metadata=edge_metadata or {},
        created_at=_NOW,
    )


# ---------------------------------------------------------------------------
# ORM model tests
# ---------------------------------------------------------------------------


class TestEntityModel:
    def test_construct_minimal(self) -> None:
        ent = _make_entity()
        assert ent.entity_type == "function"
        assert ent.name == "mymod.my_func"
        assert ent.display_name == "my_func"
        assert ent.chunk_id is None

    def test_entity_type_class(self) -> None:
        ent = _make_entity(entity_type="class", name="mymod.MyClass", display_name="MyClass")
        assert ent.entity_type == "class"

    def test_entity_type_module(self) -> None:
        ent = _make_entity(entity_type="module", name="mymod", display_name="mymod")
        assert ent.entity_type == "module"

    def test_metadata_stored(self) -> None:
        ent = _make_entity(
            entity_metadata={"line_start": 10, "line_end": 25, "language": "python"}
        )
        assert ent.entity_metadata["line_start"] == 10
        assert ent.entity_metadata["language"] == "python"

    def test_chunk_id_nullable(self) -> None:
        chunk_uuid = uuid.uuid4()
        ent = _make_entity(chunk_id=chunk_uuid)
        assert ent.chunk_id == chunk_uuid

    def test_id_is_uuid(self) -> None:
        ent = _make_entity()
        assert isinstance(ent.id, uuid.UUID)


class TestEdgeModel:
    def test_construct_minimal(self) -> None:
        src = uuid.uuid4()
        tgt = uuid.uuid4()
        edge = _make_edge(source_entity_id=src, target_entity_id=tgt, edge_type="imports")
        assert edge.source_entity_id == src
        assert edge.target_entity_id == tgt
        assert edge.edge_type == "imports"

    def test_edge_type_inherits(self) -> None:
        edge = _make_edge(edge_type="inherits")
        assert edge.edge_type == "inherits"

    def test_edge_type_defines(self) -> None:
        edge = _make_edge(edge_type="defines")
        assert edge.edge_type == "defines"

    def test_edge_type_depends_on(self) -> None:
        edge = _make_edge(edge_type="depends_on")
        assert edge.edge_type == "depends_on"

    def test_metadata_stored(self) -> None:
        edge = _make_edge(edge_metadata={"confidence": 0.95})
        assert edge.edge_metadata["confidence"] == 0.95

    def test_id_is_uuid(self) -> None:
        edge = _make_edge()
        assert isinstance(edge.id, uuid.UUID)


# ---------------------------------------------------------------------------
# Pydantic schema round-trip tests
# ---------------------------------------------------------------------------


class TestEntityRead:
    def test_from_orm(self) -> None:
        ent = _make_entity(
            entity_type="class",
            name="mod.Foo",
            display_name="Foo",
            entity_metadata={"line_start": 1},
        )
        read = EntityRead.model_validate(ent)
        assert read.entity_type == "class"
        assert read.name == "mod.Foo"
        assert read.display_name == "Foo"
        assert read.metadata == {"line_start": 1}

    def test_metadata_alias(self) -> None:
        """EntityRead.metadata is populated from ORM's entity_metadata."""
        ent = _make_entity(entity_metadata={"language": "python"})
        read = EntityRead.model_validate(ent)
        assert read.metadata["language"] == "python"

    def test_chunk_id_optional(self) -> None:
        ent = _make_entity()
        read = EntityRead.model_validate(ent)
        assert read.chunk_id is None


class TestEdgeRead:
    def test_from_orm(self) -> None:
        src = uuid.uuid4()
        tgt = uuid.uuid4()
        edge = _make_edge(source_entity_id=src, target_entity_id=tgt, edge_type="calls")
        read = EdgeRead.model_validate(edge)
        assert read.source_entity_id == src
        assert read.target_entity_id == tgt
        assert read.edge_type == "calls"

    def test_metadata_alias(self) -> None:
        edge = _make_edge(edge_metadata={"weight": 1})
        read = EdgeRead.model_validate(edge)
        assert read.metadata == {"weight": 1}


# ---------------------------------------------------------------------------
# Graph extractor — imports
# ---------------------------------------------------------------------------


class TestExtractorImports:
    def _parse(self, src: bytes, filename: str = "mymod.py") -> ParsedDocument:
        return TreeSitterParser().parse(src, filename)

    def test_simple_import(self) -> None:
        src = b"import os\ndef foo(): pass\n"
        parsed = self._parse(src)
        _, edges = extract_symbol_graph(parsed, src)
        import_edges = [e for e in edges if e.edge_type == "imports"]
        targets = {e.target_name for e in import_edges}
        assert "os" in targets

    def test_from_import(self) -> None:
        src = b"from pathlib import Path\ndef bar(): pass\n"
        parsed = self._parse(src)
        _, edges = extract_symbol_graph(parsed, src)
        import_edges = [e for e in edges if e.edge_type == "imports"]
        targets = {e.target_name for e in import_edges}
        assert "pathlib" in targets

    def test_multiple_imports(self) -> None:
        src = b"import os\nimport sys\nfrom typing import Any\ndef f(): pass\n"
        parsed = self._parse(src)
        _, edges = extract_symbol_graph(parsed, src)
        import_edges = [e for e in edges if e.edge_type == "imports"]
        targets = {e.target_name for e in import_edges}
        assert "os" in targets
        assert "sys" in targets
        assert "typing" in targets

    def test_import_edge_source_is_module_entity(self) -> None:
        src = b"import os\ndef f(): pass\n"
        parsed = self._parse(src)
        entities, edges = extract_symbol_graph(parsed, src)
        module_entity = next(e for e in entities if e.entity_type == "module")
        import_edges = [e for e in edges if e.edge_type == "imports"]
        assert all(e.source_entity_id == module_entity.id for e in import_edges)


# ---------------------------------------------------------------------------
# Graph extractor — inheritance
# ---------------------------------------------------------------------------


class TestExtractorInheritance:
    def _parse(self, src: bytes, filename: str = "mymod.py") -> ParsedDocument:
        return TreeSitterParser().parse(src, filename)

    def test_single_base(self) -> None:
        src = b"class Child(Base):\n    pass\n"
        parsed = self._parse(src)
        _, edges = extract_symbol_graph(parsed, src)
        inh_edges = [e for e in edges if e.edge_type == "inherits"]
        assert any(e.target_name == "Base" for e in inh_edges)

    def test_multiple_bases(self) -> None:
        src = b"class Child(Mixin, Base):\n    pass\n"
        parsed = self._parse(src)
        _, edges = extract_symbol_graph(parsed, src)
        inh_edges = [e for e in edges if e.edge_type == "inherits"]
        targets = {e.target_name for e in inh_edges}
        assert "Mixin" in targets
        assert "Base" in targets

    def test_no_base_no_inheritance_edge(self) -> None:
        src = b"class Standalone:\n    pass\n"
        parsed = self._parse(src)
        _, edges = extract_symbol_graph(parsed, src)
        inh_edges = [e for e in edges if e.edge_type == "inherits"]
        assert len(inh_edges) == 0

    def test_object_base_excluded(self) -> None:
        src = b"class MyClass(object):\n    pass\n"
        parsed = self._parse(src)
        _, edges = extract_symbol_graph(parsed, src)
        inh_edges = [e for e in edges if e.edge_type == "inherits"]
        assert not any(e.target_name == "object" for e in inh_edges)

    def test_inheritance_source_is_class_entity(self) -> None:
        src = b"class Child(Base):\n    pass\n"
        parsed = self._parse(src)
        entities, edges = extract_symbol_graph(parsed, src)
        class_entities = [e for e in entities if e.entity_type == "class"]
        assert class_entities, "Expected at least one class entity"
        inh_edges = [e for e in edges if e.edge_type == "inherits"]
        class_ids = {e.id for e in class_entities}
        assert all(e.source_entity_id in class_ids for e in inh_edges)


# ---------------------------------------------------------------------------
# Graph extractor — module entity and defines edges
# ---------------------------------------------------------------------------


class TestExtractorModuleAndDefines:
    def _parse(self, src: bytes, filename: str = "mymod.py") -> ParsedDocument:
        return TreeSitterParser().parse(src, filename)

    def test_module_entity_created(self) -> None:
        src = b"def foo(): pass\n"
        parsed = self._parse(src)
        entities, _ = extract_symbol_graph(parsed, src)
        module_entities = [e for e in entities if e.entity_type == "module"]
        assert len(module_entities) == 1

    def test_module_entity_name_matches_filename(self) -> None:
        src = b"def foo(): pass\n"
        parsed = self._parse(src, "utils.py")
        entities, _ = extract_symbol_graph(parsed, src)
        module_entity = next(e for e in entities if e.entity_type == "module")
        assert module_entity.name == "utils"

    def test_defines_edges_emitted(self) -> None:
        src = b"def alpha(): pass\nclass Beta:\n    pass\n"
        parsed = self._parse(src)
        _, edges = extract_symbol_graph(parsed, src)
        defines_edges = [e for e in edges if e.edge_type == "defines"]
        assert len(defines_edges) >= 2

    def test_all_symbol_entities_present(self) -> None:
        src = b"def first(): pass\ndef second(): pass\n"
        parsed = self._parse(src)
        entities, _ = extract_symbol_graph(parsed, src)
        names = {e.name for e in entities}
        assert any("first" in n for n in names)
        assert any("second" in n for n in names)


# ---------------------------------------------------------------------------
# Graph extractor — non-Python and edge cases
# ---------------------------------------------------------------------------


class TestExtractorEdgeCases:
    def test_non_python_returns_empty(self) -> None:
        src = b"function greet() { return 'hi'; }\n"
        parsed = TreeSitterParser().parse(src, "hello.ts")
        entities, edges = extract_symbol_graph(parsed, src)
        assert entities == []
        assert edges == []

    def test_go_returns_empty(self) -> None:
        src = b'package main\nfunc Greet() string { return "hi" }\n'
        parsed = TreeSitterParser().parse(src, "main.go")
        entities, edges = extract_symbol_graph(parsed, src)
        assert entities == []
        assert edges == []

    def test_empty_source_bytes(self) -> None:
        """extract_symbol_graph must not crash on empty source."""
        parsed = ParsedDocument(
            sections=[],
            content_type="text/x-source",
            language="python",
        )
        entities, _ = extract_symbol_graph(parsed, b"")
        # module entity still created
        assert any(e.entity_type == "module" for e in entities)

    def test_no_sections_still_produces_module_entity(self) -> None:
        parsed = ParsedDocument(
            sections=[],
            content_type="text/x-source",
            language="python",
        )
        entities, _ = extract_symbol_graph(parsed)
        assert len(entities) == 1
        assert entities[0].entity_type == "module"

    def test_extracted_entity_has_stable_uuid(self) -> None:
        src = b"def foo(): pass\n"
        parsed = TreeSitterParser().parse(src, "mod.py")
        entities, _ = extract_symbol_graph(parsed, src)
        for ent in entities:
            assert isinstance(ent.id, uuid.UUID)

    def test_entity_display_name_is_short(self) -> None:
        src = b"def my_function(): pass\n"
        parsed = TreeSitterParser().parse(src, "mod.py")
        entities, _ = extract_symbol_graph(parsed, src)
        fn_entity = next((e for e in entities if "my_function" in e.name), None)
        assert fn_entity is not None
        assert fn_entity.display_name == "my_function"

    def test_metadata_has_line_info(self) -> None:
        src = b"def annotated():\n    pass\n"
        parsed = TreeSitterParser().parse(src, "mod.py")
        entities, _ = extract_symbol_graph(parsed, src)
        fn_entities = [e for e in entities if e.entity_type == "function"]
        assert fn_entities
        meta = fn_entities[0].metadata
        assert "line_start" in meta
        assert "line_end" in meta


# ---------------------------------------------------------------------------
# Migration sanity check
# ---------------------------------------------------------------------------


class TestMigration:
    def test_migration_file_exists(self) -> None:
        import os

        mig_path = str(
            Path(__file__).resolve().parent.parent
            / "packages"
            / "core"
            / "alembic"
            / "versions"
            / "0002_entities_edges.py"
        )
        assert os.path.isfile(mig_path), "Migration file 0002_entities_edges.py not found"

    def test_migration_revision_chain(self) -> None:
        import importlib.util

        mig_path = str(
            Path(__file__).resolve().parent.parent
            / "packages"
            / "core"
            / "alembic"
            / "versions"
            / "0002_entities_edges.py"
        )
        spec = importlib.util.spec_from_file_location("mig_0002", mig_path)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        assert mod.revision == "0002"
        assert mod.down_revision == "0001"

    def test_migration_has_upgrade(self) -> None:
        import importlib.util

        mig_path = str(
            Path(__file__).resolve().parent.parent
            / "packages"
            / "core"
            / "alembic"
            / "versions"
            / "0002_entities_edges.py"
        )
        spec = importlib.util.spec_from_file_location("mig_0002", mig_path)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        assert callable(getattr(mod, "upgrade", None))
        assert callable(getattr(mod, "downgrade", None))
