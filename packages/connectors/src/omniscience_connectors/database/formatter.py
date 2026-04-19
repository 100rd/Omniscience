"""Schema formatter for database tables and views.

Converts raw column/constraint/comment data (as returned by information_schema
queries) into human-readable Markdown suitable for downstream ingestion.
"""

from __future__ import annotations

__all__ = ["format_table_schema"]

# Column dict keys coming from information_schema.columns
# Keys: column_name, data_type, is_nullable, column_default, ordinal_position,
#       character_maximum_length, numeric_precision, numeric_scale, udt_name
_NULL_YES = "YES"


def format_table_schema(
    table_name: str,
    columns: list[dict[str, object]],
    constraints: list[dict[str, object]] | None = None,
    comments: dict[str, str] | None = None,
) -> str:
    """Format a table's schema as Markdown.

    Args:
        table_name:  Fully-qualified table name (e.g. ``"public.users"``).
        columns:     List of column-info dicts, each with at minimum the keys
                     ``column_name``, ``data_type``, ``is_nullable``, and
                     ``column_default``.  Extra keys are silently ignored.
        constraints: Optional list of constraint dicts.  Each dict should
                     contain ``constraint_name``, ``constraint_type``, and
                     optionally ``column_name`` and ``foreign_table_name`` /
                     ``foreign_column_name``.
        comments:    Optional mapping of ``column_name`` -> comment text.
                     Pass an empty dict or ``None`` to omit comments.

    Returns:
        A Markdown string describing the table schema.
    """
    constraints = constraints or []
    comments = comments or {}

    lines: list[str] = [f"# Table: `{table_name}`\n"]

    # ------------------------------------------------------------------
    # Column table
    # ------------------------------------------------------------------
    lines.append("## Columns\n")
    lines.append("| Column | Type | Nullable | Default | Comment |")
    lines.append("|--------|------|----------|---------|---------|")

    for col in columns:
        col_name = str(col.get("column_name", ""))
        data_type = _format_type(col)
        nullable = "YES" if str(col.get("is_nullable", "YES")) == _NULL_YES else "NO"
        default = str(col.get("column_default", "") or "")
        if len(default) > 60:
            default = default[:57] + "..."
        comment = comments.get(col_name, "")
        lines.append(f"| `{col_name}` | `{data_type}` | {nullable} | {default} | {comment} |")

    lines.append("")

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    pk_cols = _constraint_cols(constraints, "PRIMARY KEY")
    uk_cols = _constraint_cols(constraints, "UNIQUE")
    fk_constraints = [c for c in constraints if str(c.get("constraint_type", "")) == "FOREIGN KEY"]
    check_constraints = [c for c in constraints if str(c.get("constraint_type", "")) == "CHECK"]

    if pk_cols or uk_cols or fk_constraints or check_constraints:
        lines.append("## Constraints\n")

        if pk_cols:
            cols_str = ", ".join(f"`{c}`" for c in pk_cols)
            lines.append(f"- **Primary Key**: {cols_str}")

        for name, cols in uk_cols.items():
            cols_str = ", ".join(f"`{c}`" for c in cols)
            lines.append(f"- **Unique** (`{name}`): {cols_str}")

        for fk in fk_constraints:
            fk_name = str(fk.get("constraint_name", ""))
            fk_col = str(fk.get("column_name", ""))
            ref_table = str(fk.get("foreign_table_name", ""))
            ref_col = str(fk.get("foreign_column_name", ""))
            lines.append(
                f"- **Foreign Key** (`{fk_name}`): `{fk_col}` → `{ref_table}`.`{ref_col}`"
            )

        for chk in check_constraints:
            chk_name = str(chk.get("constraint_name", ""))
            chk_clause = str(chk.get("check_clause", ""))
            lines.append(f"- **Check** (`{chk_name}`): `{chk_clause}`")

        lines.append("")

    # ------------------------------------------------------------------
    # Relationships (FK summary)
    # ------------------------------------------------------------------
    if fk_constraints:
        lines.append("## Relationships\n")
        for fk in fk_constraints:
            fk_col = str(fk.get("column_name", ""))
            ref_table = str(fk.get("foreign_table_name", ""))
            ref_col = str(fk.get("foreign_column_name", ""))
            lines.append(f"- `{table_name}`.`{fk_col}` references `{ref_table}`.`{ref_col}`")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _format_type(col: dict[str, object]) -> str:
    """Build a compact type string from column metadata."""
    data_type = str(col.get("data_type", ""))
    udt = str(col.get("udt_name", "") or "")

    # Prefer udt_name for PostgreSQL custom / array types
    base = udt if udt and udt != data_type and not udt.startswith("_") else data_type

    char_max = col.get("character_maximum_length")
    if char_max is not None:
        return f"{base}({char_max})"

    num_prec = col.get("numeric_precision")
    num_scale = col.get("numeric_scale")
    if num_prec is not None and isinstance(num_scale, (int, float)) and num_scale > 0:
        return f"{base}({num_prec},{num_scale})"

    return base


def _constraint_cols(
    constraints: list[dict[str, object]],
    ctype: str,
) -> dict[str, list[str]]:
    """Group column names by constraint name for the given constraint type.

    For PRIMARY KEY we collapse all names into the special key ``"__pk__"``.
    For UNIQUE we key by constraint name.
    """
    result: dict[str, list[str]] = {}
    for c in constraints:
        if str(c.get("constraint_type", "")) != ctype:
            continue
        name = str(c.get("constraint_name", "__pk__"))
        col = str(c.get("column_name", ""))
        key = "__pk__" if ctype == "PRIMARY KEY" else name
        result.setdefault(key, []).append(col)
    return result
