"""Build a demo SQLite schema to exercise seedwright.

The domain is a small order-management model chosen because it has the shapes
that break naive generators:

  employees   -- self-referencing manager_id
  customers
  products
  orders      -- FK to customers
  order_items -- FK to orders AND products (a child with two parents)

Run:  python examples/build_demo_db.py demo.db
"""

from __future__ import annotations

import sqlite3
import sys

DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE employees (
    id          INTEGER PRIMARY KEY,
    full_name   VARCHAR(80) NOT NULL,
    email       VARCHAR(120) NOT NULL UNIQUE,
    manager_id  INTEGER REFERENCES employees(id)
);

CREATE TABLE customers (
    id          INTEGER PRIMARY KEY,
    full_name   VARCHAR(80) NOT NULL,
    email       VARCHAR(120) NOT NULL UNIQUE,
    city        VARCHAR(60),
    status      VARCHAR(20) NOT NULL
);

CREATE TABLE products (
    id          INTEGER PRIMARY KEY,
    product_code VARCHAR(12) NOT NULL UNIQUE,
    name        VARCHAR(80) NOT NULL,
    price       DECIMAL(10,2) NOT NULL
);

CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    status      VARCHAR(20) NOT NULL,
    created_at  DATETIME NOT NULL
);

CREATE TABLE order_items (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    product_id  INTEGER NOT NULL REFERENCES products(id),
    quantity    INTEGER NOT NULL
);
"""


def build(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(DDL)
        conn.commit()
    finally:
        conn.close()
    print(f"built demo schema at {path}")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "demo.db")
