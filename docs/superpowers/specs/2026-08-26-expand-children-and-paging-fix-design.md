# Design: `$expand` child-collection splitting + `$top` paging-truncation fix

Component: `keboola.ex-dynamics-365-business-central`
Repo: `keboola/component-ms-dynamics365-business-central`
Branch: `feat/expand-children-and-paging-fix`
Date: 2026-08-26

Two independent changes on one branch, in two commits (both touch `src/dynamics_client.py`):

1. **Commit 1 — paging fix** (bug): tables > 2000 rows are silently truncated.
2. **Commit 2 — `$expand` feature**: pull a header endpoint's line collections into separate parent-keyed tables.

---

## Commit 1 — Paging truncation fix

### Bug
Every extraction of a collection with > 2000 rows is silently truncated to exactly 2000 — no error, just missing data.

### Root cause
`_build_query_params` sets `params["$top"] = PAGE_SIZE` (2000) on every request. Business Central does **not** return `@odata.nextLink` when `$top` is present ([microsoft/AL#6858](https://github.com/microsoft/AL/issues/6858)), so the `next_link`-following loop in `iterate_endpoint` terminates after the first page. MS docs recommend the `odata.maxpagesize` preference header instead, which preserves `@odata.nextLink`.

### Fix
1. Remove `params["$top"] = PAGE_SIZE` from `_build_query_params`.
2. Send `Prefer: odata.maxpagesize=<PAGE_SIZE>` as a request header on the data collection GET. Add an `extra_headers` param to `_request`; `_fetch_page` passes the Prefer header on **both** the first-page request and the `next_link` follow-ups (harmless to re-send; keeps page size consistent across pages).
3. The existing `@odata.nextLink` loop then pages the full result set with no other change.
4. No silent row cap anywhere. `PAGE_SIZE` (2000) is retained only as the page-size preference value; BC online max is 20000, so 2000 is fine. An explicit user-facing row limit, if ever wanted, would be a separate opt-in config field — never a hardcoded `$top`.

### Test
Mock two pages linked by `@odata.nextLink`; assert `iterate_endpoint` concatenates all rows across both pages (does not stop at page one), and assert `Prefer: odata.maxpagesize=2000` is present on the first-page request.

---

## Commit 2 — `$expand` child-collection splitting

### Problem
Every `*Lines` entity (Sales/Purchase invoice/order/credit-memo lines; dimension set lines) is "(filter required)" because in BC OData v2.0 they are child entities of a header and are not date-filterable on their own. Today, line detail requires filtering invoice-by-invoice (`documentId eq {guid}`). Users want header + lines in one run, with lines in a **separate table keyed back to the header**.

### Verified API fact
MS v2.0 docs document `$expand` on `salesInvoices`: `salesInvoices?$filter=…&$expand=salesInvoiceLines` returns each header with its lines nested, in one paged query (no N+1). **Unverified (needs live tenant):** whether BC caps/truncates a large expanded child collection *per parent*. See "Risks".

### Touch-points

**1. `dynamics_client._parse_odata_metadata`** — additionally parse `<edm:NavigationProperty>` per entity type. Split collection-valued (`Type="Collection(NS.Type)"`) from single-valued. Resolve each collection nav prop's target entity type → its keys. Expose on each `entity_set` a `nav_collections: [{"name", "target_entity_type", "keys": [...]}]`.

**2. `dynamics_client._build_query_params` + `iterate_endpoint`/`_fetch_page`** — new `expand_children: list[str] | None`. When set: emit `$expand=child1,child2`; and when `selected_columns` is also set, auto-add the parent's metadata key column(s) to `$select` so the FK value is never dropped. (`$select`/`$expand` are independent in OData v4 — children still return all fields.)

**3. `component._extract_rows`** — for each record, **before** parent-column derivation and stringify: pop each selected child array out of the record, and write it to its own `ElasticDictWriter` for table `<endpoint>_<child>` (e.g. `salesInvoices_salesInvoiceLines`), injecting the parent FK column(s) on every child row. The parent row is then written clean (no nested column). Multiple children → multiple child writers, all opened and finalised (manifest, PK, column-state) alongside the parent.

**4. New sync action `component.list_navigation_properties`** — mirrors `list_columns`; returns only the **collection-valued** nav props of the selected endpoint (single-valued expansion is out of scope — YAGNI).

**5. `configuration.Source` + `configRowSchema.json`** — add `expand_children: list[str] = []`, and one multiselect schema property fed by the new sync action.

### Decisions
- **FK column name = `parent_id`** (fixed). Holds the parent's OData key value. For BC's single-`id` key this is exact. If a parent has a *composite* key, emit `parent_<keyName>` per key (keeps it unambiguous); the common single-key case yields exactly `parent_id`.
- **Parent-key source for FK = the parent's OData metadata key(s)**, not `destination.primary_key` (user-overridable, may not be the join key). Always present in the expanded parent object.
- **Child table PK = child's own metadata key(s) + the parent FK column(s).**
- **Child load-type inherits the parent's** (`incremental` → upsert by child PK; `full` → overwrite). Child column-state tracked under its own `state["tables"][<childtable>]` key so parent state is untouched.
- **Child table name = `<endpoint>_<child>`.** Uses the source endpoint name (not the destination `table_name`) as the prefix, matching the spec's example.

### UI property text
`expand_children` (multiselect, options loaded via `list_navigation_properties`):
> Include related line items — Pull the child records belonging to each record of this endpoint into their own table, linked back by the parent's ID. Use this when a '…lines' endpoint shows '(filter required)' and you want the lines for a whole date range instead of filtering invoice-by-invoice. Select the HEADER endpoint above (e.g. Sales invoices), then pick its line collection here (e.g. salesInvoiceLines). The extractor fetches each header with its lines in a single request (OData $expand) and writes a separate <table>_<child> table keyed to the parent. Expanded children include all their fields. Leave empty to extract only this endpoint's own fields (default).

### Backwards compatibility (hard requirement)
Empty `expand_children` = zero new code path: no `$expand` param, no extra writers, parent output/manifest/state byte-identical to today. Purely additive.

## Acceptance criteria
- `salesInvoices` + `expand_children=[salesInvoiceLines]` → both `salesInvoices` and `salesInvoices_salesInvoiceLines`; child has `parent_id` FK + its own PK; no JSON-blob column on the parent.
- Incremental load on the parent still works (children ride along via `$expand`).
- Empty selection = unchanged from today (regression test proves it).
- Multi-child expand works (`salesInvoiceLines` + `dimensionSetLines`).
- Tables > 2000 rows return all rows (paging fix).

## Testing strategy (given no live creds yet)
- **Runnable now (no creds):**
  - Unit tests on pure client functions: metadata nav-prop parsing (real BC `$metadata` XML fixture), `_build_query_params` (no `$top`; `$expand` emitted; key added to `$select`).
  - Paging loop test (mock two `nextLink`-linked pages; assert full concatenation + Prefer header).
  - Mock-client extraction tests driving `Component._extract_rows` against a KBC_DATADIR: no-expand regression (byte-identical output), single-child, multi-child — asserting output tables, `parent_id` FK, child PK, and absence of nested column on parent.
- **Infra-ready, recorded later (needs creds):** full VCR functional harness — `tests/functional/`, `tests/setup/configs.json`, `VCR_SANITIZERS`, `test_functional.py` — authored via the component-developer VCR skills; cassettes recorded once a BC sandbox with sample sales invoices + lines is available. Recording is then a single scaffolder run.

## Risks / open items
- **Collection-level `$expand` paging/volume** is unverified without a live tenant. If BC truncates an expanded child collection *per parent*, expanded children would be silently partial. Mitigation: flag it loudly (log a warning if a parent's expanded array hits a suspicious round number) and treat a per-parent line-fetch fallback as a separate follow-up — do **not** silently return partial lines.
- Recording real cassettes + live `$expand` verification deferred until creds available (user to provide).
