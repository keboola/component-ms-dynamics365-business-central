import types
import unittest

import mock

from configuration import Configuration
from dynamics_client import DynamicsClient, _parse_odata_metadata

METADATA_XML = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
  <edmx:DataServices>
    <Schema Namespace="Microsoft.NAV" xmlns="http://docs.oasis-open.org/odata/ns/edm">
      <EntityType Name="salesInvoice">
        <Key><PropertyRef Name="id"/></Key>
        <Property Name="id" Type="Edm.Guid"/>
        <Property Name="number" Type="Edm.String"/>
        <NavigationProperty Name="salesInvoiceLines" Type="Collection(Microsoft.NAV.salesInvoiceLine)"/>
        <NavigationProperty Name="dimensionSetLines" Type="Collection(Microsoft.NAV.dimensionSetLine)"/>
        <NavigationProperty Name="customer" Type="Microsoft.NAV.customer"/>
      </EntityType>
      <EntityType Name="salesInvoiceLine">
        <Key><PropertyRef Name="id"/></Key>
        <Property Name="id" Type="Edm.Guid"/>
        <Property Name="sequence" Type="Edm.Int32"/>
      </EntityType>
      <EntityType Name="dimensionSetLine">
        <Key><PropertyRef Name="id"/></Key>
        <Property Name="id" Type="Edm.Guid"/>
      </EntityType>
      <EntityContainer Name="NAV">
        <EntitySet Name="salesInvoices" EntityType="Microsoft.NAV.salesInvoice"/>
        <EntitySet Name="salesInvoiceLines" EntityType="Microsoft.NAV.salesInvoiceLine"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>
"""


def make_client() -> DynamicsClient:
    config = Configuration(connection={"tenant_id": "T", "environment": "Production", "company_id": "C"})
    creds = types.SimpleNamespace(appKey="k", appSecret="s", data={})
    client = DynamicsClient(config, creds, state=None)
    client.access_token = "tok"
    return client


def fake_response(payload: dict, status: int = 200):
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


class PagingTest(unittest.TestCase):
    def test_paging_follows_nextlink_and_sends_prefer(self):
        """A >1-page collection must be fully concatenated via @odata.nextLink, not truncated."""
        client = make_client()
        pages = [
            fake_response({"value": [{"id": "1"}], "@odata.nextLink": "https://x/page2"}),
            fake_response({"value": [{"id": "2"}]}),
        ]
        with mock.patch.object(client.session, "request", side_effect=pages) as m:
            rows = list(client.iterate_endpoint("customers"))

        self.assertEqual([r["id"] for r in rows], ["1", "2"])
        first_call = m.call_args_list[0]
        self.assertNotIn("$top", first_call.kwargs.get("params") or {})
        self.assertEqual(first_call.kwargs["headers"]["Prefer"], "odata.maxpagesize=2000")

    def test_build_query_params_omits_top(self):
        client = make_client()
        params = client._build_query_params(
            selected_columns=None,
            filter_expression=None,
            incremental_field=None,
            incremental_value=None,
        )
        self.assertNotIn("$top", params)


class MetadataNavPropertiesTest(unittest.TestCase):
    def test_parse_metadata_exposes_collection_navs_only(self):
        """Only collection-valued nav properties (line collections) become expandable children."""
        parsed = _parse_odata_metadata(METADATA_XML)
        nav_collections = parsed["entity_sets"]["salesInvoices"]["nav_collections"]

        names = {n["name"] for n in nav_collections}
        self.assertEqual(names, {"salesInvoiceLines", "dimensionSetLines"})
        self.assertNotIn("customer", names)  # single-valued nav is excluded

        lines = next(n for n in nav_collections if n["name"] == "salesInvoiceLines")
        self.assertEqual(lines["keys"], ["id"])
        self.assertEqual(lines["target_entity_type"], "Microsoft.NAV.salesInvoiceLine")

    def test_list_navigation_properties(self):
        client = make_client()
        client._metadata_cache = _parse_odata_metadata(METADATA_XML)

        navs = client.list_navigation_properties("salesInvoices")
        self.assertEqual([n["name"] for n in navs], ["dimensionSetLines", "salesInvoiceLines"])  # sorted
        self.assertEqual(next(n for n in navs if n["name"] == "salesInvoiceLines")["keys"], ["id"])

    def test_entity_keys(self):
        client = make_client()
        client._metadata_cache = _parse_odata_metadata(METADATA_XML)
        self.assertEqual(client.entity_keys("salesInvoices"), ["id"])


class ExpandQueryTest(unittest.TestCase):
    def test_build_query_params_with_expand_injects_parent_key_into_select(self):
        client = make_client()
        params = client._build_query_params(
            selected_columns=["number"],
            filter_expression=None,
            incremental_field=None,
            incremental_value=None,
            expand_children=["salesInvoiceLines"],
            required_columns=["id"],
        )
        self.assertEqual(params["$expand"], "salesInvoiceLines")
        self.assertEqual(set(params["$select"].split(",")), {"number", "id"})
        self.assertNotIn("$top", params)

    def test_no_expand_means_no_expand_param(self):
        client = make_client()
        params = client._build_query_params(
            selected_columns=None,
            filter_expression=None,
            incremental_field=None,
            incremental_value=None,
        )
        self.assertNotIn("$expand", params)

    def test_iterate_endpoint_expands_and_injects_parent_key(self):
        client = make_client()
        client._metadata_cache = _parse_odata_metadata(METADATA_XML)
        with mock.patch.object(client.session, "request", return_value=fake_response({"value": []})) as m:
            list(
                client.iterate_endpoint(
                    "salesInvoices",
                    selected_columns=["number"],
                    expand_children=["salesInvoiceLines", "dimensionSetLines"],
                )
            )
        params = m.call_args_list[0].kwargs["params"]
        self.assertEqual(params["$expand"], "salesInvoiceLines,dimensionSetLines")
        self.assertEqual(set(params["$select"].split(",")), {"number", "id"})
        # Expanded pages are heavy, so a smaller maxpagesize is used to stay within memory.
        self.assertEqual(m.call_args_list[0].kwargs["headers"]["Prefer"], "odata.maxpagesize=100")

    def test_expand_uses_smaller_page_size_than_flat(self):
        client = make_client()
        client._metadata_cache = _parse_odata_metadata(METADATA_XML)
        with mock.patch.object(client.session, "request", return_value=fake_response({"value": []})) as m:
            list(client.iterate_endpoint("customers"))
        # No expand -> full page size.
        self.assertEqual(m.call_args_list[0].kwargs["headers"]["Prefer"], "odata.maxpagesize=2000")

    def test_incremental_filter_and_expand_coexist(self):
        """Incremental parent load keeps working; children ride along via $expand."""
        client = make_client()
        client._metadata_cache = _parse_odata_metadata(METADATA_XML)
        with mock.patch.object(client.session, "request", return_value=fake_response({"value": []})) as m:
            list(
                client.iterate_endpoint(
                    "salesInvoices",
                    incremental_field="lastModifiedDateTime",
                    incremental_value="2024-01-01T00:00:00Z",
                    expand_children=["salesInvoiceLines"],
                )
            )
        params = m.call_args_list[0].kwargs["params"]
        self.assertIn("lastModifiedDateTime gt", params["$filter"])
        self.assertEqual(params["$expand"], "salesInvoiceLines")


if __name__ == "__main__":
    unittest.main()
