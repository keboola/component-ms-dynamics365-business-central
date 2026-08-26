# `$expand` child-splitting + paging fix — Implementation Plan

> **For agentic workers:** Executed inline via superpowers:executing-plans with TDD. Steps use checkbox (`- [ ]`) tracking.

**Goal:** Add optional OData `$expand` of a header endpoint's line collections into separate parent-keyed output tables, and fix the `$top` paging bug that silently truncates >2000-row extractions.

**Architecture:** Two commits on one branch. Commit 1 replaces hardcoded `$top` with a `Prefer: odata.maxpagesize` header so BC returns `@odata.nextLink` and the existing loop pages fully. Commit 2 parses collection nav-properties from `$metadata`, adds `$expand` to the query, and splits expanded child arrays out of each parent record into `<endpoint>_<child>` tables with a `parent_id` FK.

**Tech Stack:** Python 3.12, `keboola.component`, `keboola.csvwriter.ElasticDictWriter`, pydantic v2, pytest, `unittest.mock`.

**Spec:** `docs/superpowers/specs/2026-08-26-expand-children-and-paging-fix-design.md`

## Global Constraints
- Backwards compat is a **hard requirement**: empty `expand_children` → byte-identical parent output/manifest/state.
- FK column name = `parent_id` (single-key case); composite parent key → `parent_<keyName>` per key.
- Child table name = `<source.endpoint>_<navName>`.
- Child load-type inherits parent; child PK = child own metadata key(s) + FK column(s).
- No silent row caps anywhere.
- `list_navigation_properties` returns **collection-valued** nav props only.

## Files
- Modify `src/dynamics_client.py` — Prefer header + paging (T1); nav-property metadata parse + `entity_keys`/`list_navigation_properties` (T2); `$expand` query params (T3).
- Modify `src/component.py` — output splitting in `_extract_rows` + helpers (T4); `list_navigation_properties` sync action (T5).
- Modify `src/configuration.py` — `Source.expand_children` (T6).
- Modify `component_config/configRowSchema.json` — multiselect property (T6).
- Create `tests/conftest.py` — put `src/` on path + shared fixtures (T1).
- Create `tests/test_dynamics_client.py` — paging, metadata, query-param unit tests (T1–T3).
- Create `tests/test_extract_rows.py` — mock-client extraction tests: regression/single/multi (T4).
- Create VCR infra under `tests/functional/`, `tests/setup/configs.json`, etc. (T7, recording deferred).

---

## Task 1 — Paging fix (Commit 1)

**Files:** Modify `src/dynamics_client.py`; Create `tests/conftest.py`, `tests/test_dynamics_client.py`.

**Interfaces produced:** `_request(..., extra_headers: dict[str,str]|None=None)`; `_build_query_params` no longer sets `$top`.

- [ ] **Step 1 — conftest** adds `src` to path and a `make_client()` helper returning a `DynamicsClient` with fake config + `access_token="tok"`.
- [ ] **Step 2 — failing test** `test_paging_follows_nextlink_and_sends_prefer`: mock `client.session.request` → two responses linked by `@odata.nextLink`; assert rows concatenate `["1","2"]`, first call has no `$top` in params and `headers["Prefer"] == "odata.maxpagesize=2000"`. Also `test_build_query_params_omits_top`.
- [ ] **Step 3 — implement:** in `_build_query_params` start `params = {}` (drop `$top`). Add `extra_headers` param to `_request` and `headers.update(extra_headers or {})`. In `_fetch_page` pass `extra_headers={"Prefer": f"odata.maxpagesize={PAGE_SIZE}"}` on both the first-page and `next_link` requests.
- [ ] **Step 4 — run tests green.**
- [ ] **Step 5 — commit** `fix: page beyond 2000 rows via odata.maxpagesize (was silently truncated by $top)`.

## Task 2 — Metadata nav-property parsing (Commit 2)

**Files:** Modify `src/dynamics_client.py`; add tests to `tests/test_dynamics_client.py`.

**Interfaces produced:**
- `_parse_odata_metadata` → each `entity_set` also has `nav_collections: [{"name","target_entity_type","keys":[...]}]`.
- `DynamicsClient.entity_keys(endpoint) -> list[str]`.
- `DynamicsClient.list_navigation_properties(endpoint) -> list[{"name","label","keys"}]` (collection-valued only, sorted).

- [ ] **Step 1 — failing test** `test_parse_metadata_nav_collections`: feed a minimal `$metadata` XML with an entity type exposing a `Collection(...)` nav prop and a single-valued nav prop; assert entity_set exposes only the collection with resolved child keys.
- [ ] **Step 2 — implement:** split `_parse_odata_metadata` into two passes — (1) collect all `entity_types` incl. `nav_collections` (regex `Collection\((.+)\)` on `NavigationProperty/@Type`); (2) build `entity_sets`, resolving each nav collection's target keys from the fully-populated `entity_types`. Add `entity_keys` and `list_navigation_properties` client methods.
- [ ] **Step 3 — run tests green.** (Commit deferred to end of T6.)

## Task 3 — `$expand` query params (Commit 2)

**Files:** Modify `src/dynamics_client.py`; add tests.

**Interfaces produced:** `_build_query_params(..., expand_children=None, required_columns=None)`; `iterate_endpoint(..., expand_children=None)` (computes parent keys, threads to `_fetch_page`).

- [ ] **Step 1 — failing test** `test_build_query_params_with_expand`: with `selected_columns=["number"]`, `expand_children=["salesInvoiceLines"]`, `required_columns=["id"]` → `$expand=="salesInvoiceLines"`, `$select` set == `{"number","id"}`, no `$top`.
- [ ] **Step 2 — implement:** in `_build_query_params` append `required_columns` to the `$select` clean-list (only when `selected_columns` truthy); add `$expand=",".join(dedup(expand_children))`. In `iterate_endpoint` add `expand_children`; when set compute `parent_keys=self.entity_keys(endpoint)` and pass `expand_children`+`required_columns=parent_keys` through `_fetch_page`.
- [ ] **Step 3 — run tests green.**

## Task 4 — Output splitting in `_extract_rows` (Commit 2)

**Files:** Modify `src/component.py`; Create `tests/test_extract_rows.py`.

**Interfaces produced:** `_build_child_specs(endpoint, expand_children) -> list[spec]`, `_split_record(record, child_nav_names)`, `_finalise_table(table, final_columns, primary_key)` (signature changed — drop `config`).

Spec dict: `{nav_name, table_name, table(OutTableDef), own_keys, fk_map{fk_col:parent_key}, fk_cols, child_pk, initial_columns, rows, final_columns}`.

Logic: build child specs + table defs; derive `first_record_keys` excluding child nav names; open parent + child `ElasticDictWriter`s in one `contextlib.ExitStack`; per record `_split_record` → write clean parent via `_process_record`, and for each child nav write each child row `_normalize_record(child)` updated with `{fk_col: stringify(parent[parent_key])}`; finalise each child table with its own PK and store `state["tables"][child_table]["columns"]`.

- [ ] **Step 1 — failing tests** in `tests/test_extract_rows.py` using a `FakeClient` injected into `Component` over a temp `KBC_DATADIR`:
  - `test_no_expand_regression`: flat records, `expand_children` unset → single table, exact expected CSV, no `parent_id`, manifest PK `["id"]`. (Byte-identical guarantee.)
  - `test_single_child_expand`: parent records each with `salesInvoiceLines: [...]` → `salesInvoices` (no `salesInvoiceLines` column) + `salesInvoices_salesInvoiceLines` with `parent_id` == parent id and child PK `["id","parent_id"]`.
  - `test_multi_child_expand`: two navs (`salesInvoiceLines`,`dimensionSetLines`) → three tables, each child keyed to parent.
- [ ] **Step 2 — implement** the refactor + helpers.
- [ ] **Step 3 — run tests green** and confirm existing `tests/test_component.py` still passes.

## Task 5 — `list_navigation_properties` sync action (Commit 2)

**Files:** Modify `src/component.py`; add a test.

- [ ] **Step 1 — failing test** `test_list_navigation_properties_action`: FakeClient returns two navs → action returns two `SelectElement`s (value=name).
- [ ] **Step 2 — implement** `@sync_action("list_navigation_properties")` mirroring `list_columns` (guard empty endpoint; wrap client errors).
- [ ] **Step 3 — run tests green.**

## Task 6 — Config + schema (Commit 2)

**Files:** Modify `src/configuration.py`, `component_config/configRowSchema.json`; add a schema-validity test.

- [ ] **Step 1** add `expand_children: list[str] = Field(default_factory=list)` to `Source`. Test: `Configuration` with and without `expand_children` both parse; default `[]`.
- [ ] **Step 2** add the `expand_children` multiselect property (async `list_navigation_properties`, `tags:true`, the spec's help text) to `source.properties`. Test: `configRowSchema.json` is valid JSON and the async action name matches the decorator.
- [ ] **Step 3 — commit** the whole feature (T2–T6) `feat: expand header line collections into separate parent-keyed tables via OData $expand`.

## Task 7 — VCR functional infra (deferred recording, Commit 3)

**Files:** `tests/functional/**`, `tests/setup/configs.json`, `VCR_SANITIZERS` in `component.py`, `tests/test_functional.py`, infra (pyproject/Dockerfile/push.yml/.gitignore as needed).

- [ ] Use the component-developer VCR skills (`vcr-test-preparer` agent) to author configs for: no-expand regression, single-child expand, multi-child expand, plus each sync action. Do **not** hand-author cassettes. Leave `secrets.json` skeleton; recording runs once BC sandbox creds exist.
- [ ] **Commit** `test: VCR functional harness for $expand + paging (cassettes pending live creds)`.

## Self-review
- **Spec coverage:** paging→T1; metadata nav parse→T2; `$expand` query→T3; output split/FK/PK→T4; sync action→T5; config+schema→T6; VCR infra→T7. All covered.
- **Placeholders:** none (recording deferral is an explicit user decision, not a TODO).
- **Type consistency:** `_finalise_table(table, final_columns, primary_key)` used consistently for parent and child; `expand_children` threaded identically through `iterate_endpoint`→`_fetch_page`→`_build_query_params`; `entity_keys`/`list_navigation_properties` names match between client and component.
