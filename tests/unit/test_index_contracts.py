"""Recipe identity and inventory contracts do not import optional engines."""
from dataclasses import FrozenInstanceError
import json
import unittest

from graphmind.rag.components.index_contracts import IndexInventory, IndexRecipe


class IndexContractTests(unittest.TestCase):
    def test_recipe_identity_is_canonical_and_detached_from_caller_mutation(self):
        parameters = {"model": {"revision": "immutable", "precision": "float32"}, "size": 512}
        first = IndexRecipe.create("graphmind", "dense-v1", parameters)
        reordered = IndexRecipe.create("graphmind", "dense-v1",
                                       {"size": 512, "model": {"precision": "float32", "revision": "immutable"}})
        self.assertEqual(first, reordered)
        self.assertEqual(first.fingerprint, reordered.fingerprint)
        self.assertEqual(IndexRecipe.deserialize(first.serialized), first)
        parameters["model"]["precision"] = "changed-after-submit"
        self.assertEqual(first, reordered)
        with self.assertRaises(FrozenInstanceError):
            first.revision = "changed"

    def test_index_identity_separates_source_installation_engine_and_recipe(self):
        recipe = IndexRecipe.create("graphmind", "dense-v1", {"size": 512})
        identities = {recipe.index_id("install", "v1"), recipe.index_id("other", "v1"),
                      recipe.index_id("install", "v2"),
                      IndexRecipe.create("graphmind", "dense-v2", {"size": 512}).index_id("install", "v1"),
                      IndexRecipe.create("graphmind", "dense-v1", {"size": 256}).index_id("install", "v1"),
                      IndexRecipe.create("tree_navigation", "dense-v1", {"size": 512}).index_id("install", "v1")}
        self.assertEqual(len(identities), 6)
        self.assertTrue(all(len(value) == 64 for value in identities))

    def test_malformed_or_nonfinite_recipe_values_are_rejected(self):
        for engine, revision, value in (("../bad", "v1", "{}"), ("graphmind", "../bad", "{}"),
                                        ("graphmind", "v1", "[]"), ("graphmind", "v1", '{"x":NaN}')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                IndexRecipe(engine, revision, value)
        value = json.loads(IndexRecipe.create("graphmind", "v1", {}).serialized)
        value["schema_version"] = 99
        with self.assertRaises(ValueError):
            IndexRecipe.deserialize(json.dumps(value))

    def test_inventory_preserves_order_and_rejects_duplicate_or_ambiguous_units(self):
        first = IndexInventory.from_unit_ids(["page:1", "page:2"])
        self.assertEqual(first.unit_count, 2)
        self.assertNotEqual(first, IndexInventory.from_unit_ids(["page:2", "page:1"]))
        self.assertEqual(IndexInventory.from_unit_ids([]).unit_count, 0)
        for identifiers in (["page:1", "page:1"], ["page:1\npage:2"], "page:1", {"page:1"}):
            with self.subTest(identifiers=identifiers), self.assertRaises(ValueError):
                IndexInventory.from_unit_ids(identifiers)


if __name__ == "__main__":
    unittest.main()
