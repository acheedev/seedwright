"""Command-line interface.

    python -m seedwright --db demo.db --rows 50 --out seed.sql
    python -m seedwright --db demo.db --table-rows users=100,orders=400 --seed 7
    python -m seedwright --db demo.db --format csv --out-dir ./seed_csv
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from .config import app_config, config_value, dialect_config, load_config
from .dialects import SQLiteDialect
from .emit import to_csv, to_sql
from .engine import GenerationEngine

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_LOG_LEVELS = {"quiet": 0, "info": 1, "debug": 2}


def _parse_per_table(spec: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for pair in spec.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise argparse.ArgumentTypeError(
                f"--table-rows expects name=count, got {pair!r}"
            )
        name, count = pair.split("=", 1)
        name = name.strip()
        count = count.strip()
        if not name:
            raise argparse.ArgumentTypeError("--table-rows table name cannot be empty")
        try:
            out[name] = _parse_nonnegative_int(count)
        except argparse.ArgumentTypeError as exc:
            raise argparse.ArgumentTypeError(
                f"--table-rows count for {name!r} must be a non-negative integer"
            ) from exc
    return out


def _parse_nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected a non-negative integer, got {value!r}"
        ) from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError(
            f"expected a non-negative integer, got {value!r}"
        )
    return parsed


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="seedwright",
        description="Generate referentially-correct synthetic data from a live schema.",
    )
    p.add_argument(
        "--dialect", choices=["sqlite", "oracle"], default="sqlite",
        help="which database to introspect (default: sqlite)",
    )
    p.add_argument(
        "--config",
        default="seedwright.ini",
        help="path to config file with per-dialect sections (default: seedwright.ini)",
    )
    p.add_argument("--db", help="path to a SQLite database file (dialect=sqlite)")
    p.add_argument(
        "--oracle-user",
        help="Oracle username override; otherwise [oracle].user from config",
    )
    p.add_argument(
        "--oracle-password",
        help="Oracle password override; otherwise [oracle].password from config",
    )
    p.add_argument(
        "--rows",
        type=_parse_nonnegative_int,
        default=25,
        help="default rows per table",
    )
    p.add_argument(
        "--table-rows",
        type=_parse_per_table,
        default={},
        help="per-table overrides, e.g. users=100,orders=400",
    )
    p.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible output")
    p.add_argument(
        "--format", choices=["sql", "csv"], default="sql", help="output format"
    )
    p.add_argument("--out", help="output file for SQL (default: stdout)")
    p.add_argument("--out-dir", help="output directory for CSV (one file per table)")
    return p


def _make_dialect(args):
    try:
        config = load_config(args.config)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc

    if args.dialect == "oracle":
        oracle_config = dialect_config(config, "oracle")
        user = config_value(oracle_config, "user", args.oracle_user)
        password = config_value(oracle_config, "password", args.oracle_password)
        dsn = _oracle_dsn(oracle_config)
        missing = [name for name, value in (
            ("[oracle].user or --oracle-user", user),
            ("[oracle].password or --oracle-password", password),
            ("[oracle].host", oracle_config.get("host")),
            ("[oracle].service_name", oracle_config.get("service_name")),
        ) if not value]
        if missing:
            raise SystemExit("dialect=oracle requires: " + ", ".join(missing))
        from .dialects import OracleDialect
        assert user is not None
        assert password is not None
        assert dsn is not None
        return OracleDialect(user, password, dsn)

    if not args.db:
        raise SystemExit("dialect=sqlite requires --db")
    return SQLiteDialect(args.db)


def _log_level(config, override: str | None = None) -> int:
    raw = override if override is not None else app_config(config).get("log_level", "quiet")
    raw = (raw or "quiet").lower()
    if raw not in _LOG_LEVELS:
        raise SystemExit(
            "[seedwright].log_level must be one of: "
            + ", ".join(sorted(_LOG_LEVELS))
        )
    return _LOG_LEVELS[raw]


def _log(message: str, level: int, stream, required: int = 1) -> None:
    if level >= required:
        print(message, file=stream)


def _oracle_dsn(config) -> str | None:
    host = config.get("host")
    service_name = config.get("service_name")
    if not host or not service_name:
        return None
    port = config.get("port", "1521")
    return f"{host}:{port}/{service_name}"


def _validate_table_rows(schema, per_table: dict[str, int]) -> None:
    unknown = sorted(name for name in per_table if name not in schema.tables)
    if unknown:
        known = ", ".join(sorted(schema.tables))
        raise SystemExit(
            "unknown table(s) in --table-rows: "
            + ", ".join(unknown)
            + (f". known tables: {known}" if known else "")
        )


def _safe_csv_filename(table_name: str, used: set[str]) -> str:
    stem = _SAFE_FILENAME_RE.sub("_", table_name).strip("._")
    if not stem:
        stem = "table"
    candidate = stem + ".csv"
    i = 2
    while candidate.lower() in used:
        candidate = f"{stem}_{i}.csv"
        i += 1
    used.add(candidate.lower())
    return candidate


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc
    log_level = _log_level(config)
    log_stream = sys.stderr if args.format == "sql" and not args.out else sys.stdout

    _log(f"seedwright: loading {args.dialect} dialect", log_level, log_stream)
    dialect = _make_dialect(args)
    _log("seedwright: introspecting schema", log_level, log_stream)
    schema = dialect.introspect()
    if len(schema) == 0:
        print("no tables found in database", file=sys.stderr)
        return 1
    _validate_table_rows(schema, args.table_rows)
    _log(f"seedwright: found {len(schema)} table(s)", log_level, log_stream)
    _log(
        "seedwright: tables: " + ", ".join(sorted(schema.tables)),
        log_level,
        log_stream,
        required=2,
    )

    row_plan = {name: args.table_rows.get(name, args.rows) for name in schema.tables}
    _log(
        "seedwright: row plan: "
        + ", ".join(f"{name}={count}" for name, count in row_plan.items()),
        log_level,
        log_stream,
    )

    _log("seedwright: generating rows", log_level, log_stream)
    engine = GenerationEngine(
        schema,
        default_rows=args.rows,
        per_table=args.table_rows,
        seed=args.seed,
    )
    data = engine.generate()
    total_rows = sum(len(r) for r in data.values())
    _log(f"seedwright: generated {total_rows} row(s)", log_level, log_stream)

    if args.format == "sql":
        _log("seedwright: rendering SQL", log_level, log_stream)
        sql = to_sql(schema, data, dialect)
        if args.out:
            _log(f"seedwright: writing SQL to {args.out}", log_level, log_stream)
            with open(args.out, "w") as fh:
                fh.write(sql)
            print(f"wrote {total_rows} rows to {args.out}", file=sys.stderr)
        else:
            print(sql)
    else:
        out_dir = args.out_dir or "."
        _log(f"seedwright: writing CSV files to {out_dir}", log_level, log_stream)
        os.makedirs(out_dir, exist_ok=True)
        used_filenames: set[str] = set()
        for tname, rows in data.items():
            filename = _safe_csv_filename(tname, used_filenames)
            path = os.path.join(out_dir, filename)
            _log(
                f"seedwright: writing {tname} ({len(rows)} row(s)) -> {filename}",
                log_level,
                log_stream,
                required=2,
            )
            with open(path, "w", newline="") as fh:
                fh.write(to_csv(tname, rows))
        print(f"wrote {len(data)} CSV files to {out_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
