"""Build a demo ITIL service desk schema in SQLite.

A deliberately ITIL-shaped model: support groups, agents, requesters, a CMDB of
configuration items, a self-referencing category tree, priorities, and the
process records that tie them together — incidents, problems, changes, and an
incident work log.

It's chosen to stress the engine: `incidents` alone carries seven foreign keys
(a heavy fan-in), `categories` is self-referencing, and the dependency chain
runs several tables deep, so the topological sort has real work to do.

Run:  python examples/build_servicedesk_db.py servicedesk.db
"""

from __future__ import annotations

import sqlite3
import sys

DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE support_groups (
    id              INTEGER PRIMARY KEY,
    group_name      VARCHAR(60) NOT NULL UNIQUE,
    is_active       BOOLEAN NOT NULL
);

CREATE TABLE priorities (
    id              INTEGER PRIMARY KEY,
    priority_code   VARCHAR(4) NOT NULL UNIQUE,   -- P1..P4
    priority_name   VARCHAR(30) NOT NULL,
    response_mins   INTEGER NOT NULL,
    resolve_mins    INTEGER NOT NULL
);

CREATE TABLE requesters (
    id              INTEGER PRIMARY KEY,
    full_name       VARCHAR(80) NOT NULL,
    email           VARCHAR(120) NOT NULL UNIQUE,
    department      VARCHAR(60)
);

CREATE TABLE categories (
    id                  INTEGER PRIMARY KEY,
    category_name       VARCHAR(60) NOT NULL,
    parent_category_id  INTEGER REFERENCES categories(id)
);

CREATE TABLE agents (
    id                  INTEGER PRIMARY KEY,
    full_name           VARCHAR(80) NOT NULL,
    email               VARCHAR(120) NOT NULL UNIQUE,
    support_group_id    INTEGER NOT NULL REFERENCES support_groups(id)
);

CREATE TABLE configuration_items (
    id              INTEGER PRIMARY KEY,
    ci_name         VARCHAR(80) NOT NULL UNIQUE,
    ci_type         VARCHAR(30) NOT NULL,         -- Server, Application, Database...
    ci_status       VARCHAR(20) NOT NULL,
    owner_agent_id  INTEGER REFERENCES agents(id)
);

CREATE TABLE problems (
    id                  INTEGER PRIMARY KEY,
    problem_number      VARCHAR(12) NOT NULL UNIQUE,  -- PRB0000001
    assigned_agent_id   INTEGER REFERENCES agents(id),
    priority_id         INTEGER NOT NULL REFERENCES priorities(id),
    status              VARCHAR(20) NOT NULL,
    opened_at           DATETIME NOT NULL
);

CREATE TABLE incidents (
    id                  INTEGER PRIMARY KEY,
    incident_number     VARCHAR(12) NOT NULL UNIQUE,  -- INC0000001
    requester_id        INTEGER NOT NULL REFERENCES requesters(id),
    assigned_agent_id   INTEGER REFERENCES agents(id),
    support_group_id    INTEGER NOT NULL REFERENCES support_groups(id),
    category_id         INTEGER NOT NULL REFERENCES categories(id),
    priority_id         INTEGER NOT NULL REFERENCES priorities(id),
    ci_id               INTEGER REFERENCES configuration_items(id),
    problem_id          INTEGER REFERENCES problems(id),
    status              VARCHAR(20) NOT NULL,
    opened_at           DATETIME NOT NULL,
    resolved_at         DATETIME
);

CREATE TABLE changes (
    id                  INTEGER PRIMARY KEY,
    change_number       VARCHAR(12) NOT NULL UNIQUE,  -- CHG0000001
    requested_by_agent_id INTEGER NOT NULL REFERENCES agents(id),
    ci_id               INTEGER REFERENCES configuration_items(id),
    risk                VARCHAR(20) NOT NULL,
    status              VARCHAR(20) NOT NULL,
    scheduled_at        DATETIME
);

CREATE TABLE incident_worklog (
    id              INTEGER PRIMARY KEY,
    incident_id     INTEGER NOT NULL REFERENCES incidents(id),
    agent_id        INTEGER NOT NULL REFERENCES agents(id),
    note            VARCHAR(200) NOT NULL,
    logged_at       DATETIME NOT NULL
);
"""


def build(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(DDL)
        conn.commit()
    finally:
        conn.close()
    print(f"built ITIL service desk schema at {path}")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "servicedesk.db")
