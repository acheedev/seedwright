import datetime as dt
import unittest

from seedwright.engine import GenerationEngine
from seedwright.generators import FeasibilityError, ValueFactory
from seedwright.model import Column, ForeignKey, Schema, Table


def col(name, **kw):
    return Column(name=name, type=kw.pop("type", "integer"), **kw)


class DistinctAllocationTests(unittest.TestCase):
    def test_integers_are_distinct_and_in_range(self):
        c = col("seq", type="integer", unique=True)  # generic range 0..10000
        vals = ValueFactory(seed=1).distinct_values(c, 200)
        self.assertEqual(len(vals), 200)
        self.assertEqual(len(set(vals)), 200)
        self.assertTrue(all(0 <= v <= 10_000 for v in vals))

    def test_unbounded_integer_widens_to_fit(self):
        # generic int range is 0..10000; ask for more than that, no precision cap
        c = col("n", type="integer", unique=True)
        vals = ValueFactory(seed=2).distinct_values(c, 15_000)
        self.assertEqual(len(set(vals)), 15_000)

    def test_precision_capped_integer_fails_fast(self):
        # NUMBER(2,0) -> at most 100 distinct values
        c = col("code", type="integer", unique=True,
                numeric_precision=2, numeric_scale=0)
        with self.assertRaises(FeasibilityError):
            ValueFactory(seed=3).distinct_values(c, 500)

    def test_boolean_capacity_is_two(self):
        c = col("flag", type="boolean", unique=True)
        self.assertEqual(len(ValueFactory(seed=4).distinct_values(c, 2)), 2)
        with self.assertRaises(FeasibilityError):
            ValueFactory(seed=4).distinct_values(c, 3)

    def test_dates_distinct_and_window_enforced(self):
        c = col("d", type="date", unique=True)
        vals = ValueFactory(seed=5).distinct_values(c, 500)
        self.assertEqual(len(set(vals)), 500)
        self.assertTrue(all(isinstance(v, dt.date) for v in vals))
        with self.assertRaises(FeasibilityError):
            ValueFactory(seed=5).distinct_values(c, 902)

    def test_small_text_domain_fails_fast(self):
        # VARCHAR(1) token domain is ~36 values
        c = col("ch", type="text", unique=True, max_length=1)
        with self.assertRaises(FeasibilityError):
            ValueFactory(seed=6).distinct_values(c, 200)

    def test_text_codes_are_distinct(self):
        c = col("code", type="text", unique=True, max_length=8)
        vals = ValueFactory(seed=7).distinct_values(c, 300)
        self.assertEqual(len(set(vals)), 300)
        self.assertTrue(all(len(v) <= 8 for v in vals))

    def test_reproducible_under_seed(self):
        c = col("seq", type="integer", unique=True)
        a = ValueFactory(seed=42).distinct_values(c, 100)
        b = ValueFactory(seed=42).distinct_values(c, 100)
        self.assertEqual(a, b)


class EngineUniquenessTests(unittest.TestCase):
    def test_unique_text_column_all_distinct(self):
        s = Schema()
        s.add(Table("things", [
            col("id", type="integer", primary_key=True, nullable=False),
            col("code", type="text", unique=True, nullable=False, max_length=8),
        ]))
        rows = GenerationEngine(s, default_rows=150, seed=1).generate()["things"]
        codes = [r["code"] for r in rows]
        self.assertEqual(len(set(codes)), 150)

    def test_impossible_unique_boolean_fails_fast(self):
        s = Schema()
        s.add(Table("flags", [
            col("id", type="integer", primary_key=True, nullable=False),
            col("on", type="boolean", unique=True, nullable=False),
        ]))
        with self.assertRaises(FeasibilityError):
            GenerationEngine(s, default_rows=10, seed=1).generate()

    def test_unique_notnull_fk_needs_enough_parents(self):
        s = Schema()
        s.add(Table("parent", [col("id", type="integer", primary_key=True, nullable=False)]))
        s.add(Table("child", [
            col("id", type="integer", primary_key=True, nullable=False),
            col("parent_id", type="integer", unique=True, nullable=False,
                foreign_key=ForeignKey("parent", "id")),
        ]))
        with self.assertRaises(ValueError):
            GenerationEngine(s, per_table={"parent": 3, "child": 10}, seed=1).generate()


if __name__ == "__main__":
    unittest.main()
