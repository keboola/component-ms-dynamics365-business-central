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


class ComponentTestBase(unittest.TestCase):
    """Shared harness - builds a Component over a temp KBC_DATADIR with an injected fake client.

    Holds no tests itself, so the concrete test classes below don't inherit (and re-run) each
    other's cases.
    """

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
        # Component() reconfigures root logging in __init__, so build it before any assertLogs block.
        self._component(parameters, fake_client).run()

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


class ExtractRowsTest(ComponentTestBase):
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

    def test_forced_parent_key_not_leaked_into_restricted_parent(self):
        """With a custom column selection, the parent key force-selected for the child FK must not
        appear as an empty column on the parent table - but the child FK still gets the real value."""
        params = {
            "connection": {"tenant_id": "T", "environment": "Production", "company_id": "C"},
            "source": {
                "endpoint": "salesInvoices",
                "selected_columns": ["number"],
                "expand_children": ["salesInvoiceLines"],
            },
            "destination": {"table_name": "", "load_type": "full_load", "primary_key": ["number"]},
        }
        # FakeClient yields id even though the user didn't select it (mirrors the real force-select).
        records = [{"id": "inv1", "number": "S-1", "salesInvoiceLines": [{"id": "l1", "sequence": 1}]}]
        nav = [{"name": "salesInvoiceLines", "label": "Sales invoice lines", "keys": ["id"]}]
        self._run(params, FakeClient(records, nav_props=nav, keys=["id"]))

        parent_rows = self._read_table("salesInvoices")
        self.assertEqual(parent_rows, [{"number": "S-1"}])
        self.assertNotIn("id", parent_rows[0])
        # child FK is still populated from the (unrestricted) parent record
        self.assertEqual(
            self._read_table("salesInvoices_salesInvoiceLines"),
            [{"id": "l1", "sequence": "1", "parent_id": "inv1"}],
        )

    def test_child_table_prefixed_with_custom_parent_table_name(self):
        """Child tables follow the parent's effective output-table name, not the raw endpoint, so a
        custom destination.table_name keeps parent and children consistent (and avoids collisions)."""
        params = {
            "connection": {"tenant_id": "T", "environment": "Production", "company_id": "C"},
            "source": {"endpoint": "salesInvoices", "expand_children": ["salesInvoiceLines"]},
            "destination": {"table_name": "my_invoices", "load_type": "full_load", "primary_key": ["id"]},
        }
        records = [{"id": "inv1", "number": "S-1", "salesInvoiceLines": [{"id": "l1", "sequence": 1}]}]
        nav = [{"name": "salesInvoiceLines", "label": "Sales invoice lines", "keys": ["id"]}]
        self._run(params, FakeClient(records, nav_props=nav, keys=["id"]))

        self.assertEqual(self._out_tables(), ["my_invoices", "my_invoices_salesInvoiceLines"])
        self.assertEqual(
            self._read_table("my_invoices_salesInvoiceLines"),
            [{"id": "l1", "sequence": "1", "parent_id": "inv1"}],
        )

    def test_duplicate_expand_children_deduplicated(self):
        """A duplicated expand entry (possible in hand-edited config) must not double the child
        table or its rows."""
        params = {
            "connection": {"tenant_id": "T", "environment": "Production", "company_id": "C"},
            "source": {"endpoint": "salesInvoices", "expand_children": ["salesInvoiceLines", "salesInvoiceLines"]},
            "destination": {"table_name": "", "load_type": "full_load", "primary_key": ["id"]},
        }
        records = [{"id": "inv1", "number": "S-1", "salesInvoiceLines": [{"id": "l1", "sequence": 1}]}]
        nav = [{"name": "salesInvoiceLines", "label": "Sales invoice lines", "keys": ["id"]}]
        self._run(params, FakeClient(records, nav_props=nav, keys=["id"]))

        self.assertEqual(self._out_tables(), ["salesInvoices", "salesInvoices_salesInvoiceLines"])
        self.assertEqual(
            self._read_table("salesInvoices_salesInvoiceLines"),
            [{"id": "l1", "sequence": "1", "parent_id": "inv1"}],
        )

    # --- expanded-collection truncation signal ----------------------------------

    def test_nested_odata_nextlink_not_leaked_and_warns(self):
        """A nested `<nav>@odata.nextLink` (BC truncated the child collection) must not pollute the
        parent table and must raise a warning rather than silently drop lines."""
        params = {
            "connection": {"tenant_id": "T", "environment": "Production", "company_id": "C"},
            "source": {"endpoint": "salesInvoices", "expand_children": ["salesInvoiceLines"]},
            "destination": {"table_name": "", "load_type": "full_load", "primary_key": ["id"]},
        }
        records = [{
            "id": "inv1", "number": "S-1",
            "salesInvoiceLines": [{"id": "l1", "sequence": 1}],
            "salesInvoiceLines@odata.nextLink": "https://api/.../salesInvoiceLines?$skiptoken=x",
        }]
        nav = [{"name": "salesInvoiceLines", "label": "Sales invoice lines", "keys": ["id"]}]

        comp = self._component(params, FakeClient(records, nav_props=nav, keys=["id"]))
        with self.assertLogs(level="WARNING") as logs:
            comp.run()

        parent_rows = self._read_table("salesInvoices")
        self.assertEqual(parent_rows, [{"id": "inv1", "number": "S-1"}])
        self.assertNotIn("salesInvoiceLines@odata.nextLink", parent_rows[0])
        self.assertEqual(
            self._read_table("salesInvoices_salesInvoiceLines"),
            [{"id": "l1", "sequence": "1", "parent_id": "inv1"}],
        )
        self.assertTrue(any("truncat" in line.lower() for line in logs.output))

    def test_child_without_own_key_warns(self):
        """A child collection with no resolvable metadata key would dedupe to parent_id only."""
        params = {
            "connection": {"tenant_id": "T", "environment": "Production", "company_id": "C"},
            "source": {"endpoint": "salesInvoices", "expand_children": ["extraLines"]},
            "destination": {"table_name": "", "load_type": "incremental_load", "primary_key": ["id"]},
        }
        records = [{"id": "inv1", "number": "S-1", "extraLines": [{"foo": "bar"}]}]
        nav = [{"name": "extraLines", "label": "Extra lines", "keys": []}]

        comp = self._component(params, FakeClient(records, nav_props=nav, keys=["id"]))
        with self.assertLogs(level="WARNING") as logs:
            comp.run()

        self.assertEqual(self._primary_key("salesInvoices_extraLines"), ["parent_id"])
        self.assertTrue(any("parent_id" in line for line in logs.output))

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


class SyncActionTest(ComponentTestBase):
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
