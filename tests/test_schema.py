import json
import unittest
from pathlib import Path

from component import Component
from configuration import Configuration

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "component_config" / "configRowSchema.json"


# component_config/ is not copied into the CI Docker test image (the canonical Dockerfile ships only
# src/tests/scripts), so these file-reading checks run during local dev / pre-commit and skip in CI.
@unittest.skipUnless(SCHEMA_PATH.exists(), "component_config/configRowSchema.json not available in this environment")
class SchemaTest(unittest.TestCase):
    def test_row_schema_is_valid_json(self):
        json.loads(SCHEMA_PATH.read_text())

    def test_expand_children_property_wired_to_sync_action(self):
        schema = json.loads(SCHEMA_PATH.read_text())
        prop = schema["properties"]["source"]["properties"]["expand_children"]
        action = prop["options"]["async"]["action"]

        self.assertEqual(action, "list_navigation_properties")
        self.assertEqual(prop["type"], "array")
        # the async action must resolve to a real sync action on the component
        self.assertTrue(hasattr(Component, action))


class ConfigurationTest(unittest.TestCase):
    def test_expand_children_defaults_to_empty(self):
        cfg = Configuration(connection={"tenant_id": "T"}, source={"endpoint": "customers"})
        self.assertEqual(cfg.source.expand_children, [])

    def test_expand_children_parsed(self):
        cfg = Configuration(
            connection={"tenant_id": "T"},
            source={"endpoint": "salesInvoices", "expand_children": ["salesInvoiceLines"]},
        )
        self.assertEqual(cfg.source.expand_children, ["salesInvoiceLines"])


if __name__ == "__main__":
    unittest.main()
