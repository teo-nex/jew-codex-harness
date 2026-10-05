import unittest
from harness import catalog, onboard, core


class CatalogTests(unittest.TestCase):
    def test_choose_exact_advertised_id(self):
        rows = catalog.normalize({"data": [{"id": "z/model"}, {"id": "a/model"}]})
        self.assertEqual(catalog.choose(lambda _: "2", rows, "Choose"), "z/model")
        with self.assertRaises(core.InstallError):
            catalog.choose(lambda _: "3", rows, "Choose")

    def test_missing_and_alias_family_are_warned_without_identity_claim(self):
        config = {"providers": [{"id": "plus", "transport": "omniroute",
                                 "models": {"gpt-6-sol": "codex/gpt-5.6-sol-high", "gpt-6-astra": "absent"}}]}
        rows = catalog.normalize({"data": [{"id": "codex/gpt-5.6-sol-high", "secret": "must-not-print"}]})
        self.assertNotIn("secret", str(rows))
        self.assertEqual({w["warning"] for w in catalog.warnings(config, rows)},
                         {"family_substitution", "not_advertised"})

    def test_wizard_uses_catalog_not_typed_id(self):
        answers = iter(("1", "my-route", "", "", "1", "", "unknown"))
        config = onboard._provider_sequence(lambda _: next(answers), [{"id": "vendor/exact"}])
        self.assertEqual(config["providers"][0]["model"], "vendor/exact")

    def test_catalog_rejects_duplicate_and_malformed_models(self):
        for value in ({}, {"data": [None]}, {"data": [{"id": "a"}, {"id": "a"}]}):
            with self.assertRaises(ValueError):
                catalog.normalize(value)
