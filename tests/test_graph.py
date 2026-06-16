import unittest

from seedwright.graph import CyclicDependencyError, dependency_plan, topological_order
from seedwright.model import Column, ForeignKey, Schema, Table


def col(name, **kw):
    return Column(name=name, type=kw.pop("type", "integer"), **kw)


class TopoOrderTests(unittest.TestCase):
    def _schema(self):
        s = Schema()
        s.add(Table("customers", [col("id", primary_key=True)]))
        s.add(Table("orders", [
            col("id", primary_key=True),
            col("customer_id", foreign_key=ForeignKey("customers", "id")),
        ]))
        s.add(Table("order_items", [
            col("id", primary_key=True),
            col("order_id", foreign_key=ForeignKey("orders", "id")),
        ]))
        return s

    def test_parents_before_children(self):
        order = topological_order(self._schema())
        self.assertLess(order.index("customers"), order.index("orders"))
        self.assertLess(order.index("orders"), order.index("order_items"))

    def test_self_reference_is_not_a_cycle(self):
        s = Schema()
        s.add(Table("employees", [
            col("id", primary_key=True),
            col("manager_id", nullable=True,
                foreign_key=ForeignKey("employees", "id")),
        ]))
        self.assertEqual(topological_order(s), ["employees"])

    def test_true_cycle_is_detected(self):
        s = Schema()
        s.add(Table("a", [col("id", primary_key=True),
                          col("b_id", foreign_key=ForeignKey("b", "id"))]))
        s.add(Table("b", [col("id", primary_key=True),
                          col("a_id", foreign_key=ForeignKey("a", "id"))]))
        with self.assertRaises(CyclicDependencyError):
            topological_order(s)

    def test_dependency_plan_defers_nullable_cycle_edge(self):
        s = Schema()
        s.add(Table("a", [col("id", primary_key=True),
                          col("b_id", nullable=True,
                              foreign_key=ForeignKey("b", "id"))]))
        s.add(Table("b", [col("id", primary_key=True),
                          col("a_id", nullable=False,
                              foreign_key=ForeignKey("a", "id"))]))

        plan = dependency_plan(s)

        self.assertEqual(plan.order, ["a", "b"])
        self.assertEqual(len(plan.deferred_fks), 1)
        self.assertEqual(plan.deferred_fks[0].table, "a")
        self.assertEqual(plan.deferred_fks[0].columns, ("b_id",))

    def test_dependency_plan_refuses_cycle_with_no_nullable_edge(self):
        s = Schema()
        s.add(Table("a", [col("id", primary_key=True),
                          col("b_id", nullable=False,
                              foreign_key=ForeignKey("b", "id"))]))
        s.add(Table("b", [col("id", primary_key=True),
                          col("a_id", nullable=False,
                              foreign_key=ForeignKey("a", "id"))]))

        with self.assertRaises(CyclicDependencyError):
            dependency_plan(s)

    def test_unknown_parent_raises(self):
        s = Schema()
        s.add(Table("orders", [col("id", primary_key=True),
                               col("c_id", foreign_key=ForeignKey("ghost", "id"))]))
        with self.assertRaises(KeyError):
            topological_order(s)

    def test_order_is_deterministic(self):
        s = self._schema()
        self.assertEqual(topological_order(s), topological_order(s))


if __name__ == "__main__":
    unittest.main()
