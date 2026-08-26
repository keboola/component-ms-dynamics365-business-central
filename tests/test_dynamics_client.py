import types
import unittest

import mock

from configuration import Configuration
from dynamics_client import DynamicsClient


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


if __name__ == "__main__":
    unittest.main()
