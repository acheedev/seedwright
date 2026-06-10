"""The one interface you implement to support a new database.

A dialect does exactly two things:

* `introspect()` — read a live schema and return our `Schema` model.
* `quote_literal()` — render a Python value as a SQL literal for that engine.

That is the whole contract. The generator, the dependency sort, and the row
builder are all dialect-free, so adding Oracle means writing an `all_tab_columns`
/ `all_cons_columns` introspector and a literal-quoter — and nothing else moves.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..model import Column, Schema


class Dialect(ABC):
    name: str = "abstract"

    @abstractmethod
    def introspect(self) -> Schema:
        """Read the connected database and return the internal schema model."""

    @abstractmethod
    def quote_identifier(self, identifier: str) -> str:
        """Quote a table or column name for this engine."""

    @abstractmethod
    def quote_literal(self, value: Any) -> str:
        """Render a Python value as a SQL literal for this engine."""

    def quote_column_literal(self, value: Any, column: Column) -> str:
        """Render a value with access to the originating column metadata."""
        return self.quote_literal(value)
