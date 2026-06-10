# seedwright

Generate synthetic test data straight from a live database schema — with the
foreign keys actually wired up. Point it at a database, get back an `INSERT`
script (or CSV) where every child row references a parent row that exists.

The hard part of fake data isn't names that look real. It's that a row in
`order_items` has to point at an `order` that exists, which points at a
`customer` that exists. Hand-rolled generators and naive `faker` scripts get
this wrong constantly. seedwright treats referential integrity as the whole
point, not an afterthought.

## Quickstart

No dependencies required. With Python 3.10+:

```bash
# build a demo SQLite schema (order-management domain)
python examples/build_demo_db.py demo.db

# generate a referentially-correct seed script
python -m seedwright --db demo.db \
    --table-rows customers=15,orders=30,order_items=60,products=10,employees=8 \
    --seed 7 --out seed.sql
```

The output loads cleanly into a database with foreign keys enforced. That's the
guarantee — verified in the test suite by running SQLite's own
`PRAGMA foreign_key_check` against the generated data and asserting zero
violations.

CSV instead of SQL:

```bash
python -m seedwright --db demo.db --format csv --out-dir ./seed_csv
```

By default seedwright introspects and generates data for every user table it
finds. To restrict a run, put one table name per line in a file:

```text
# seedwright.tables
customers
orders
order_items
```

Then pass it with `--tables-file`:

```bash
python -m seedwright --db demo.db --tables-file seedwright.tables --out seed.sql
```

The list must include referenced parent tables. For example, if `orders`
references `customers`, include both tables so the generated data can still
satisfy foreign keys.

Review and guarded apply, when the selected dialect supports it:

```bash
# For the SQLite demo, create this throwaway database first,
# with the same table definitions.
python examples/build_demo_db.py throwaway.db

python -m seedwright --db demo.db \
    --validate-db throwaway.db \
    --review-sql \
    --apply
```

`--review-sql` prints the generated SQL before validation. `--apply` runs the
script against the validation database inside a rolled-back transaction, checks
foreign keys, and then prompts before inserting into the source database. The
tool does not create or drop the throwaway database; you own that setup. The
core CLI does not contain database-specific execution logic; validation and
apply are dialect hooks.

For Postgres, pass a validation connection string instead:

```bash
python -m seedwright --dialect postgres --config seedwright.ini \
    --validate-db "host=localhost dbname=app_throwaway user=scott password=tiger" \
    --review-sql \
    --apply
```

A richer demo — an ITIL service desk schema (CMDB, incidents, problems,
changes, a self-referencing category tree, and an `incidents` table with seven
foreign keys):

```bash
python examples/build_servicedesk_db.py servicedesk.db
python -m seedwright --db servicedesk.db --seed 5 \
    --table-rows requesters=40,agents=15,configuration_items=30,incidents=120,incident_worklog=200
```

## How it works

```
live schema
  -> Dialect.introspect()   reads tables, columns, types, PKs, FKs, uniques
  -> optional table filter  keeps all tables by default, or a FK-closed allow-list
  -> topological_order()    sorts tables so parents come before children
  -> GenerationEngine       generates rows; FK columns draw from real parent PKs
  -> emit.to_sql / to_csv   renders, parents first, ready to load
```

The generation engine never writes to a live database. A dialect turns a real
schema into an internal `Schema` model; everything after that — the dependency
sort, value generation, FK wiring, emission — is database-agnostic. If you opt
into `--apply`, the CLI validates the emitted SQL against your validation target
first, then asks before writing to the source database.

Schema shapes it handles:

- foreign-key chains (`order_items -> orders -> customers`)
- a child with multiple parents (`order_items -> orders` **and** `products`)
- composite primary keys and composite foreign keys
- self-references (`employees.manager_id -> employees.id`)
- nullable foreign keys (sometimes filled, sometimes `NULL`)
- single-column unique constraints (no collisions)
- true cross-table cycles — **detected and reported**, not silently mangled

## Values that read like real data

Generation is type-driven, sharpened by column-name heuristics: `email` looks
like an email, `price` / `amount` is money, `created_at` is a recent timestamp,
`status` is a small enum, and free-text notes/descriptions use lorem-style
sentences. Two optional accelerators, each with a standard-library
fallback so the tool runs with nothing installed:

- **NumPy** (`pip install -e ".[numpy]"`) — vectorized, skewed numeric
  distributions (lognormal money, small-skewed counts) generated a column at a
  time instead of value by value.
- **Faker** (`pip install -e ".[faker]"`) — richer names, addresses, and
  phrases.

`--seed N` makes any run fully reproducible.

## Dialects

A dialect is the only database-specific code. It implements schema/quoting
methods and may implement guarded validation/apply hooks:

```python
class Dialect(ABC):
    def introspect(self) -> Schema: ...
    def quote_identifier(self, name) -> str: ...
    def quote_literal(self, value) -> str: ...
    def validate_script(self, validation_target, table_names, sql) -> ValidationResult: ...
    def apply_script(self, sql) -> None: ...
```

The generator, the dependency sort, and the emitter never change — they depend
only on this interface. The CLI also delegates validation/apply to this
interface. Three dialects ship:

- **`SQLiteDialect`** — introspects via `PRAGMA`. Zero setup, used by the demos
  and the tests.
- **`OracleDialect`** — introspects the `USER_*` data dictionary views
  (`user_tab_columns`, `user_constraints`, `user_cons_columns`) and renders
  Oracle literals (`TO_DATE(...)`, ANSI `DATE` literals, quoted uppercase
  identifiers). The live-connection code is thin; the type mapping and schema
  assembly are pure functions, unit-tested without an Oracle instance.
- **`PostgresDialect`** — introspects `information_schema` for tables, columns,
  primary keys, unique constraints, and foreign keys in one schema. It renders
  Postgres literals and implements guarded validation/apply through psycopg.

```bash
pip install -e ".[oracle]"                # pulls python-oracledb
cp seedwright.example.ini seedwright.ini
# edit [oracle].host and [oracle].service_name for your environment
python -m seedwright --dialect oracle --config seedwright.ini \
    --rows 50 --seed 7 --out seed.sql
```

```bash
pip install -e ".[postgres]"              # pulls psycopg
cp seedwright.example.ini seedwright.ini
# edit [postgres].host, dbname, user, password, and schema for your environment
python -m seedwright --dialect postgres --config seedwright.ini \
    --rows 50 --seed 7 --out seed.sql
```

Connection settings use one section per dialect:

```ini
[oracle]
user = scott
password = tiger
host = oracle.example.test
port = 1521
service_name = FREEPDB1

[postgres]
host = postgres.example.test
port = 5432
dbname = app
user = scott
password = tiger
schema = public
```

For Postgres, you can also replace the individual connection fields with a
single `[postgres].dsn` value if you prefer a psycopg connection string.

Global CLI settings live in `[seedwright]`:

```ini
[seedwright]
log_level = info  # quiet, info, or debug
```

SQLite, Oracle, and Postgres support are implemented. New dialects only need to
implement the dialect interface; the generation pipeline stays shared.

## Tests

```bash
python -m unittest discover -s tests     # standard library, no install
# or
pip install -e ".[dev]" && pytest
```

Coverage: dependency ordering (including self-reference and cycle detection),
value generation (types, uniqueness, seed reproducibility), end-to-end FK
integrity against a loaded in-memory database, and emitter escaping.

## Scope

Implemented: SQLite, Oracle, and Postgres introspection, the full generation
pipeline, composite primary and foreign keys, SQL and CSV output, table
allow-lists, reproducible seeding, and dialect-hooked guarded apply. SQLite and
Postgres implement guarded apply hooks today; Oracle can add its own
validation/apply behavior without changing the core CLI.

Deliberately out of scope for now: CHECK-constraint-aware generation and
reference/lookup-table awareness. The last one is worth calling out — column
heuristics fill values by type and name, so a `priority_code` becomes a random
token and an `incident_number` becomes word salad rather than `P1` or
`INC0000001`. The tool doesn't know your domain's conventions or your lookup
values; it guarantees referential integrity, not semantic realism in reference
tables. It's a focused tool, not a platform.

## License

MIT
