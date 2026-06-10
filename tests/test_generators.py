import unittest
from decimal import Decimal

from seedwright.generators import ValueFactory
from seedwright.model import Column


class GeneratorTests(unittest.TestCase):
    def test_seed_is_reproducible(self):
        c = Column("amount", type="numeric", nullable=False)
        a = ValueFactory(seed=42).batch(c, 20)
        b = ValueFactory(seed=42).batch(c, 20)
        self.assertEqual(a, b)

    def test_email_heuristic(self):
        c = Column("email", type="text", nullable=False, unique=True)
        f = ValueFactory(seed=1)
        vals = [f.one(c, i) for i in range(5)]
        self.assertTrue(all("@example.com" in v for v in vals))
        self.assertEqual(len(set(vals)), len(vals))  # unique

    def test_code_heuristic_returns_token(self):
        c = Column("product_code", type="text", nullable=False, unique=True)
        f = ValueFactory(seed=3)
        value = f.one(c, 0)
        self.assertEqual(len(value), 8)
        self.assertTrue(value.isalnum())

    def test_code_respects_max_length(self):
        c = Column("priority_code", type="text", nullable=False, unique=True, max_length=4)
        value = ValueFactory(seed=3).one(c, 0)
        self.assertLessEqual(len(value), 4)
        self.assertTrue(value.isalnum())

    def test_email_respects_max_length_when_possible(self):
        c = Column("email", type="text", nullable=False, unique=True, max_length=16)
        value = ValueFactory(seed=1).one(c, 0)
        self.assertLessEqual(len(value), 16)
        self.assertIn("@example.com", value)

    def test_very_short_email_respects_max_length(self):
        c = Column("email", type="text", nullable=False, unique=True, max_length=6)
        value = ValueFactory(seed=1).one(c, 0)
        self.assertLessEqual(len(value), 6)

    def test_status_respects_max_length(self):
        c = Column("status", type="text", nullable=False, max_length=6)
        value = ValueFactory(seed=4).one(c, 0)
        self.assertLessEqual(len(value), 6)
        self.assertIn(value, {"ACTIVE", "CLOSED"})

    def test_fallback_text_respects_max_length(self):
        c = Column("note", type="text", nullable=False, max_length=5)
        value = ValueFactory(seed=7).one(c, 0)
        self.assertLessEqual(len(value), 5)

    def test_fallback_text_uses_lorem_style_sentence(self):
        c = Column("description", type="text", nullable=False)
        value = ValueFactory(seed=7).one(c, 0)
        self.assertTrue(value.endswith("."))
        self.assertGreaterEqual(len(value.split()), 8)
        self.assertNotIn("alpha", value.lower())

    def test_numeric_money_is_rounded(self):
        c = Column("price", type="numeric", nullable=False)
        for v in ValueFactory(seed=5).batch(c, 30):
            self.assertEqual(round(v, 2), v)

    def test_numeric_precision_and_scale_are_respected(self):
        c = Column(
            "price",
            type="numeric",
            nullable=False,
            numeric_precision=5,
            numeric_scale=2,
        )
        vals = ValueFactory(seed=5).batch(c, 30)
        self.assertTrue(all(isinstance(v, Decimal) for v in vals))
        self.assertTrue(all(Decimal("0.00") <= v <= Decimal("999.99") for v in vals))
        self.assertTrue(all(v.as_tuple().exponent == -2 for v in vals))

    def test_integer_precision_is_respected(self):
        c = Column(
            "qty",
            type="integer",
            nullable=False,
            numeric_precision=3,
            numeric_scale=0,
        )
        vals = ValueFactory(seed=8).batch(c, 30)
        self.assertTrue(all(0 <= v <= 999 for v in vals))

    def test_not_null_pk_never_none(self):
        c = Column("id", type="integer", nullable=False, primary_key=True)
        f = ValueFactory(seed=9)
        self.assertTrue(all(f.one(c, i) is not None for i in range(50)))

    def test_boolean_is_python_bool(self):
        c = Column("is_active", type="boolean", nullable=False)
        f = ValueFactory(seed=2)
        self.assertTrue(all(type(f.one(c, i)) is bool for i in range(20)))

    def test_not_null_blob_gets_bytes(self):
        c = Column("payload", type="blob", nullable=False)
        value = ValueFactory(seed=2).one(c, 0)
        self.assertIsInstance(value, bytes)
        self.assertGreater(len(value), 0)


if __name__ == "__main__":
    unittest.main()
