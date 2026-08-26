import csv
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import mock

from component import Component

AUTHORIZATION = {
    "oauth_api": {
        "id": "OAUTH_API_ID",
        "credentials": {
            "id": "main",
            "authorizedFor": "Myself",
            "creator": {"id": "1", "description": "test"},
            "created": "2016-01-31 00:13:30",
            "#data": '{"refresh_token":"r"}',
            "oauthVersion": "2.0",
            "appKey": "k",
            "#appSecret": "s",
        },
    }
}


class FakeClient:
    """In-memory stand-in for DynamicsClient - no HTTP, deterministic records."""

    def __init__(self, records, nav_props=None, keys=None):
        self._records = records
        self._nav_props = nav_props or []
        self._keys = keys or ["id"]
        self.tokens_changed = False

    def iterate_endpoint(self, endpoint, **kwargs):
        for record in self._records:
            yield dict(record)

    def entity_keys(self, endpoint):
        return list(self._keys)

    def list_navigation_properties(self, endpoint):
        return list(self._nav_props)


class ExtractRowsTest(unittest.TestCase):
    def setUp(self):
        self.datadir = tempfile.mkdtemp()
        for sub in ("in/tables", "in/files", "out/tables", "out/files"):
            (Path(self.datadir) / sub).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.datadir, ignore_errors=True)

    def _component(self, parameters, fake_client, action="run"):
        config = {
            "storage": {"input": {"files": [], "tables": []}, "output": {"files": [], "tables": []}},
            "parameters": parameters,
            "action": action,
            "authorization": AUTHORIZATION,
        }
        (Path(self.datadir) / "config.json").write_text(json.dumps(config))
        with mock.patch.dict(os.environ, {"KBC_DATADIR": self.datadir}):
            comp = Component()
            comp.client = fake_client
            return comp

    def _run(self, parameters, fake_client):
        comp = self._component(parameters, fake_client)
        comp.run()

    def _out_tables(self):
        tables_dir = Path(self.datadir) / "out" / "tables"
        return sorted(p.name for p in tables_dir.iterdir() if not p.name.endswith(".manifest"))

    def _read_table(self, name):
        with open(Path(self.datadir) / "out" / "tables" / name, newline="") as f:
            return list(csv.DictReader(f))

    def _read_manifest(self, name):
        return json.loads((Path(self.datadir) / "out" / "tables" / f"{name}.manifest").read_text())

    def _primary_key(self, name):
        """Extract the primary-key columns from the typed manifest schema."""
        schema = self._read_manifest(name)["schema"]
        return [col["name"] for col in schema if col.get("primary_key")]

    # --- backwards compatibility -------------------------------------------------

    def test_no_expand_regression(self):
        params = {
            "connection": {"tenant_id": "T", "environment": "Production", "company_id": "C"},
            "source": {"endpoint": "customers", "selected_columns": []},
            "destination": {"table_name": "customers", "load_type": "full_load", "primary_key": ["id"]},
        }
        records = [{"id": "c1", "displayName": "Alice"}, {"id": "c2", "displayName": "Bob"}]
        self._run(params, FakeClient(records))

        self.assertEqual(self._out_tables(), ["customers"])
        rows = self._read_table("customers")
        self.assertEqual(rows, [{"id": "c1", "displayName": "Alice"}, {"id": "c2", "displayName": "Bob"}])
        self.assertNotIn("parent_id", rows[0])
        self.assertEqual(self._primary_key("customers"), ["id"])

    # --- single child expand -----------------------------------------------------

    def test_single_child_expand(self):
        params = {
            "connection": {"tenant_id": "T", "environment": "Production", "company_id": "C"},
            "source": {"endpoint": "salesInvoices", "selected_columns": [], "expand_children": ["salesInvoiceLines"]},
            "destination": {"table_name": "", "load_type": "full_load", "primary_key": ["id"]},
        }
        records = [
            {"id": "inv1", "number": "S-1", "salesInvoiceLines": [
                {"id": "l1", "sequence": 1}, {"id": "l2", "sequence": 2}]},
            {"id": "inv2", "number": "S-2", "salesInvoiceLines": [{"id": "l3", "sequence": 1}]},
        ]
        nav = [{"name": "salesInvoiceLines", "label": "Sales invoice lines", "keys": ["id"]}]
        self._run(params, FakeClient(records, nav_props=nav, keys=["id"]))

        self.assertEqual(self._out_tables(), ["salesInvoices", "salesInvoices_salesInvoiceLines"])

        parent_rows = self._read_table("salesInvoices")
        self.assertEqual(parent_rows, [{"id": "inv1", "number": "S-1"}, {"id": "inv2", "number": "S-2"}])
        self.assertNotIn("salesInvoiceLines", parent_rows[0])

        child_rows = self._read_table("salesInvoices_salesInvoiceLines")
        self.assertEqual(child_rows, [
            {"id": "l1", "sequence": "1", "parent_id": "inv1"},
            {"id": "l2", "sequence": "2", "parent_id": "inv1"},
            {"id": "l3", "sequence": "1", "parent_id": "inv2"},
        ])
        self.assertEqual(set(self._primary_key("salesInvoices_salesInvoiceLines")), {"id", "parent_id"})

    # --- multi child expand ------------------------------------------------------

    def test_multi_child_expand(self):
        params = {
            "connection": {"tenant_id": "T", "environment": "Production", "company_id": "C"},
            "source": {
                "endpoint": "salesInvoices",
                "selected_columns": [],
                "expand_children": ["salesInvoiceLines", "dimensionSetLines"],
            },
            "destination": {"table_name": "", "load_type": "full_load", "primary_key": ["id"]},
        }
        records = [{
            "id": "inv1", "number": "S-1",
            "salesInvoiceLines": [{"id": "l1", "sequence": 1}],
            "dimensionSetLines": [{"id": "d1", "code": "DEPT"}],
        }]
        nav = [
            {"name": "salesInvoiceLines", "label": "Sales invoice lines", "keys": ["id"]},
            {"name": "dimensionSetLines", "label": "Dimension set lines", "keys": ["id"]},
        ]
        self._run(params, FakeClient(records, nav_props=nav, keys=["id"]))

        self.assertEqual(self._out_tables(), [
            "salesInvoices", "salesInvoices_dimensionSetLines", "salesInvoices_salesInvoiceLines"])

        lines = self._read_table("salesInvoices_salesInvoiceLines")
        self.assertEqual(lines, [{"id": "l1", "sequence": "1", "parent_id": "inv1"}])
        dims = self._read_table("salesInvoices_dimensionSetLines")
        self.assertEqual(dims, [{"id": "d1", "code": "DEPT", "parent_id": "inv1"}])


class SyncActionTest(ExtractRowsTest):
    def test_list_navigation_properties_returns_collection_navs(self):
        params = {
            "connection": {"tenant_id": "T", "environment": "Production", "company_id": "C"},
            "source": {"endpoint": "salesInvoices"},
            "destination": {"load_type": "full_load"},
        }
        nav = [
            {"name": "salesInvoiceLines", "label": "Sales invoice lines", "keys": ["id"]},
            {"name": "dimensionSetLines", "label": "Dimension set lines", "keys": ["id"]},
        ]
        comp = self._component(params, FakeClient([], nav_props=nav), action="list_navigation_properties")
        result = comp.list_navigation_properties()
        self.assertEqual([e.value for e in result], ["salesInvoiceLines", "dimensionSetLines"])

    def test_list_navigation_properties_requires_endpoint(self):
        params = {
            "connection": {"tenant_id": "T", "environment": "Production", "company_id": "C"},
            "source": {"endpoint": ""},
            "destination": {"load_type": "full_load"},
        }
        comp = self._component(params, FakeClient([]), action="list_navigation_properties")
        # The @sync_action wrapper turns a UserException into exit(1) at runtime.
        with self.assertRaises(SystemExit):
            comp.list_navigation_properties()


if __name__ == "__main__":
    unittest.main()
