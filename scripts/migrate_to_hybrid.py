#!/usr/bin/env python3
"""CLI entry point for the pgvector -> Neo4j + Qdrant migration (#108).

Usage examples::

    # Dry-run (counts only, no writes)
    python scripts/migrate_to_hybrid.py --dry-run

    # Full migration, default batch size, all workspaces
    python scripts/migrate_to_hybrid.py

    # One source, larger batch, resume from progress file
    python scripts/migrate_to_hybrid.py \\
        --source-id 11111111-1111-1111-1111-111111111111 \\
        --batch-size 1000 \\
        --resume

    # Verification-only pass on an already-migrated data set
    python scripts/migrate_to_hybrid.py --verify

Safety notes
------------

* Every write goes through ``Neo4jGraphStore`` /
  ``QdrantVectorStore``; the CLI never touches Cypher or Qdrant
  points directly.
* A missing ``tenant_id`` on a pgvector ``Source`` aborts the run
  unless ``--default-workspace-id UUID`` is passed.  This is the
  migration-level ACL gate (ADR-0006 §ACL).
* The progress file is JSON; see
  ``packages/index/src/omniscience_index/migration/progress.py``
  for its shape.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Annotated

import structlog
import typer
from omniscience_core.config import Settings
from omniscience_core.db import create_async_engine, create_session_factory
from omniscience_embeddings.factory import create_embedding_provider
from omniscience_index.migration import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_PROGRESS_FILE,
    MigrationConfig,
    MigrationRunner,
    VerificationReport,
    VerifyRunner,
)
from omniscience_index.migration.runner import MigrationReport
from omniscience_index.stores.neo4j_store import (
    Neo4jGraphStore,
    Neo4jStoreConfig,
)
from omniscience_index.stores.qdrant_config import QdrantConfig
from omniscience_index.stores.qdrant_store import QdrantVectorStore
from omniscience_retrieval.adapters.pgvector_graph import PgVectorGraphStore
from rich.console import Console
from rich.table import Table

log = structlog.get_logger(__name__)
console = Console()

app = typer.Typer(
    name="migrate_to_hybrid",
    help="One-shot migration: pgvector -> Neo4j + Qdrant (issue #108).",
    add_completion=False,
)


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    """Parse ``value`` as UUID; return ``None`` when empty/None."""
    if value is None or value == "":
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise typer.BadParameter(f"not a valid UUID: {value!r}") from exc


@app.command()
def main(
    workspace_id: Annotated[
        str | None,
        typer.Option(
            "--workspace-id",
            help="Restrict migration to a single workspace (UUID).",
        ),
    ] = None,
    source_id: Annotated[
        str | None,
        typer.Option(
            "--source-id",
            help="Restrict migration to a single source (UUID).",
        ),
    ] = None,
    default_workspace_id: Annotated[
        str | None,
        typer.Option(
            "--default-workspace-id",
            help=(
                "Fallback workspace for sources whose Source.tenant_id is NULL. "
                "Without this flag, such sources abort the run (ACL invariant)."
            ),
        ),
    ] = None,
    batch_size: Annotated[
        int,
        typer.Option(
            "--batch-size",
            help="Streaming insert batch size.",
            min=1,
        ),
    ] = DEFAULT_BATCH_SIZE,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Read-only: report counts and schema, do not write.",
        ),
    ] = False,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume",
            help="Skip sources already listed in the progress file.",
        ),
    ] = False,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify",
            help=(
                "After the write phase, run counts + round-trip + "
                "cross-workspace isolation checks.  Combine with "
                "--verify-only to skip writes entirely."
            ),
        ),
    ] = False,
    verify_only: Annotated[
        bool,
        typer.Option(
            "--verify-only",
            help="Run verification against an already-migrated dataset.",
        ),
    ] = False,
    progress_file: Annotated[
        Path,
        typer.Option(
            "--progress-file",
            help="Path to the JSON progress file for --resume.",
        ),
    ] = Path(DEFAULT_PROGRESS_FILE),
) -> None:
    """Run the pgvector -> Neo4j + Qdrant migration."""
    config = MigrationConfig(
        workspace_id=_parse_uuid(workspace_id),
        source_id=_parse_uuid(source_id),
        default_workspace_id=_parse_uuid(default_workspace_id),
        batch_size=batch_size,
        dry_run=dry_run,
        resume=resume,
        verify_only=verify_only,
        progress_file=progress_file,
    )
    exit_code = asyncio.run(_run_async(config=config, verify_after=verify))
    raise typer.Exit(code=exit_code)


async def _run_async(*, config: MigrationConfig, verify_after: bool) -> int:
    """Top-level async workflow: open stores, run, print report, close."""
    settings = Settings()
    engine = create_async_engine(settings)
    session_factory = create_session_factory(engine)

    graph_store = Neo4jGraphStore(config=Neo4jStoreConfig.from_settings(settings))
    vector_store = QdrantVectorStore(
        config=QdrantConfig(
            host=settings.qdrant_host,
            grpc_port=settings.qdrant_grpc_port,
            http_port=settings.qdrant_http_port,
            api_key=settings.qdrant_api_key,
            https=settings.qdrant_https,
            prefer_grpc=settings.qdrant_prefer_grpc,
            timeout_seconds=settings.qdrant_timeout_seconds,
        ),
        embedding_provider=create_embedding_provider(settings),
    )

    try:
        await graph_store.connect()
        await vector_store.connect()

        if not config.verify_only:
            runner = MigrationRunner(
                config=config,
                session_factory=session_factory,
                graph_store=graph_store,
                vector_store=vector_store,
            )
            report = await runner.run()
            _print_migration_report(report)

        if verify_after or config.verify_only:
            verify_runner = VerifyRunner(
                config=config,
                session_factory=session_factory,
                graph_store=graph_store,
                vector_store=vector_store,
                pgvector_graph_get_entity=PgVectorGraphStore(session_factory=session_factory),
            )
            verification = await verify_runner.run()
            _print_verification_report(verification)
            if not verification.passed:
                return 2
        return 0
    finally:
        await graph_store.close()
        await vector_store.close()
        await engine.dispose()


def _print_migration_report(report: MigrationReport) -> None:
    """Render a per-source table + totals to the console."""
    table = Table(title="Migration summary" + (" (dry-run)" if report.dry_run else ""))
    table.add_column("Source", style="cyan")
    table.add_column("Workspace", style="magenta")
    table.add_column("Entities", justify="right")
    table.add_column("Edges", justify="right")
    table.add_column("Chunks", justify="right")
    table.add_column("Docs", justify="right")
    table.add_column("Status", style="yellow")
    for s in report.source_reports:
        table.add_row(
            f"{s.source_name} ({s.source_id})",
            str(s.workspace_id),
            str(s.entities_copied),
            str(s.edges_copied),
            str(s.chunks_copied),
            str(s.documents_scanned),
            (s.skip_reason or "migrated") if s.skipped else "migrated",
        )
    console.print(table)
    console.print(
        f"[green]Totals[/green]: {report.sources_processed} sources processed, "
        f"{report.sources_skipped} skipped, "
        f"{report.total_entities} entities, {report.total_edges} edges, "
        f"{report.total_chunks} chunks."
    )


def _print_verification_report(report: VerificationReport) -> None:
    """Render the verification matrix to the console."""
    status = "[green]PASS[/green]" if report.passed else "[red]FAIL[/red]"
    console.print(f"Verification: {status}")
    if report.count_mismatches:
        tbl = Table(title="Count mismatches")
        tbl.add_column("Source")
        tbl.add_column("Kind")
        tbl.add_column("pgvector", justify="right")
        tbl.add_column("hybrid", justify="right")
        for m in report.count_mismatches:
            tbl.add_row(
                f"{m.source_name} ({m.source_id})",
                m.kind,
                str(m.pgvector),
                str(m.hybrid),
            )
        console.print(tbl)
    if report.round_trip_failures:
        tbl = Table(title="Round-trip failures")
        tbl.add_column("Entity")
        tbl.add_column("Fields")
        for f in report.round_trip_failures:
            tbl.add_row(f.entity_name, ", ".join(f.field_diffs))
        console.print(tbl)
    if report.isolation_breaches:
        tbl = Table(title="Isolation breaches (CRITICAL)")
        tbl.add_column("Probe workspace")
        tbl.add_column("Leaked from")
        tbl.add_column("Chunk")
        for b in report.isolation_breaches:
            tbl.add_row(
                str(b.probe_workspace_id),
                str(b.leaked_from_workspace_id),
                str(b.chunk_id),
            )
        console.print(tbl)
    if not report.retrieval_overlap_pass:
        console.print(
            f"[red]Retrieval overlap below threshold: {report.retrieval_overlap_ratio:.3f}[/red]"
        )


if __name__ == "__main__":
    sys.exit(app() or 0)
