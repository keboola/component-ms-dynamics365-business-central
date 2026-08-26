import json
import logging
from contextlib import ExitStack
from datetime import datetime, timezone
from itertools import chain
from typing import Any

from keboola.component.base import ComponentBase, sync_action
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import SelectElement
from keboola.csvwriter import ElasticDictWriter

from configuration import Configuration
from dynamics_client import (
    ENDPOINTS_REQUIRING_FILTERS,
    DynamicsAuthenticationError,
    DynamicsClient,
    DynamicsClientError,
    DynamicsRateLimitError,
)


class Component(ComponentBase):
    def __init__(self) -> None:
        super().__init__()

        # Mute info logging for sync actions
        action = self.configuration.action
        if action and action != "run":
            logging.getLogger().setLevel(logging.CRITICAL)

        self.config: Configuration = Configuration(**self.configuration.parameters)
        self.state: dict[str, Any] = self._load_state()
        self.client: DynamicsClient = DynamicsClient(self.config, self.configuration.oauth_credentials, self.state)

    def run(self) -> None:
        try:
            rows_written, final_columns = self._extract_rows()
            self._sync_tokens_if_needed()
            self._update_row_state(final_columns)

            self.write_state_file(self.state)
            logging.info("Extraction finished. Rows written: %s", rows_written)
        except DynamicsClientError as e:
            raise self._wrap_client_error(e)

    def _sync_tokens_if_needed(self) -> None:
        if self.client.tokens_changed:
            self.state["oauth"] = self.client.oauth_state

    def _update_row_state(self, columns: list[str]) -> None:
        """Update state with last run timestamp and columns."""
        row_state = self._ensure_row_state()
        row_state["last_run"] = datetime.now(timezone.utc).isoformat()

        if columns:
            row_state["columns"] = columns

    def _load_state(self) -> dict[str, Any]:
        state = self.get_state_file()
        return state if isinstance(state, dict) else {}

    def _extract_rows(self) -> tuple[int, list[str]]:
        """Extract data from endpoint and write to CSV, managing incremental state."""
        row_state = self._ensure_row_state()
        previous_columns: list[str] = row_state.get("columns", [])
        last_run: str | None = row_state.get("last_run")

        incremental_field = (
            self.config.source.incremental_field or None if self.config.destination.incremental else None
        )
        incremental_value = None
        if incremental_field:
            incremental_value = last_run or (self.config.source.initial_since or None)

        # Validate that primary key and incremental field are in selected columns if defined
        self._validate_column_selection(incremental_field)

        endpoint = self.config.source.endpoint
        expand_children = list(self.config.source.expand_children or [])
        child_specs = self._build_child_specs(endpoint, expand_children)
        child_nav_names = {spec["nav_name"] for spec in child_specs}
        # The parent key(s) are force-selected when expanding so the child foreign key is populated,
        # even if the user didn't pick them - but they must not surface as empty parent columns.
        parent_keys = self.client.entity_keys(endpoint) if expand_children else []

        logging.info("Starting extraction for endpoint '%s'.", endpoint)

        iterator = self.client.iterate_endpoint(
            endpoint,
            include_company_scope=True,
            selected_columns=self.config.source.selected_columns,
            filter_expression=self.config.source.filter_expression or None,
            incremental_field=incremental_field,
            incremental_value=incremental_value,
            custom_url_suffix=self.config.source.custom_url_suffix or None,
            expand_children=expand_children or None,
        )
        records_iter = iter(iterator)
        first_record = next(records_iter, None)

        # Expanded child collections (and their nested @odata.* annotations) must not leak into the
        # parent table's columns. "@odata." never matches a real field - top-level control fields are
        # already stripped upstream, so this is a no-op when nothing is expanded.
        first_record_keys = [
            key
            for key in (first_record.keys() if first_record else [])
            if key not in child_nav_names and "@odata." not in key
        ]
        preferred_columns = self.config.source.selected_columns or []
        base_columns = list(dict.fromkeys(chain(previous_columns, preferred_columns, first_record_keys)))
        if preferred_columns:
            # Custom selection: the parent table shows only what the user chose. Drop parent keys that
            # were force-selected solely to feed the child foreign key (they'd be empty columns here).
            forced_only = set(parent_keys) - set(preferred_columns)
            base_columns = [col for col in base_columns if col not in forced_only]

        table = self.create_out_table_definition(
            self.config.destination.table_name or endpoint,
            incremental=self.config.destination.incremental,
            primary_key=self.config.destination.primary_key or None,
            columns=base_columns or None,
            has_header=True,
        )

        total_rows = 0
        has_custom_selection = bool(self.config.source.selected_columns)
        record_stream = chain([first_record], records_iter) if first_record else records_iter

        with ExitStack() as stack:
            writer = stack.enter_context(ElasticDictWriter(table.full_path, list(base_columns)))
            if writer.fieldnames:
                writer.writeheader()

            child_writers = {}
            for spec in child_specs:
                child_writer = stack.enter_context(
                    ElasticDictWriter(spec["table"].full_path, list(spec["initial_columns"]))
                )
                if child_writer.fieldnames:
                    child_writer.writeheader()
                child_writers[spec["nav_name"]] = child_writer

            truncated_children: set[str] = set()
            for record in record_stream:
                parent_part, children, truncated = self._split_record(record, child_nav_names)
                self._process_record(writer, parent_part, preferred_columns, has_custom_selection)
                total_rows += 1
                self._write_children(child_specs, child_writers, parent_part, children)
                truncated_children |= truncated

            final_columns = list(writer.fieldnames) if writer.fieldnames else []
            for spec in child_specs:
                spec["final_columns"] = list(child_writers[spec["nav_name"]].fieldnames or [])

        self._finalise_table(table, final_columns, self.config.destination.primary_key)
        for spec in child_specs:
            self._finalise_table(spec["table"], spec["final_columns"], spec["child_pk"])
            self.state.setdefault("tables", {}).setdefault(spec["table_name"], {})["columns"] = spec["final_columns"]

        if total_rows == 0:
            logging.info("No records returned for endpoint '%s'. Output file left empty.", endpoint)
        else:
            logging.info("Finished endpoint '%s'. Rows written: %s.", endpoint, total_rows)
        for spec in child_specs:
            logging.info("Child table '%s': %s rows written.", spec["table_name"], spec["rows"])

        for nav_name in sorted(truncated_children):
            logging.warning(
                "Business Central truncated the expanded '%s' collection for one or more parent "
                "records (nested @odata.nextLink present); table '%s' may be missing rows. Narrow "
                "the run (e.g. a shorter incremental window) so each parent's lines fit one page.",
                nav_name,
                f"{endpoint}_{nav_name}",
            )

        return total_rows, final_columns

    def _process_record(
        self,
        writer: ElasticDictWriter,
        record: dict[str, Any],
        preferred_columns: list[str],
        restrict_to_selection: bool,
    ) -> None:
        """Normalize and write a single record."""
        normalized = self._normalize_record(record)

        if restrict_to_selection:
            # Only include selected columns
            row = {col: normalized.get(col, "") for col in preferred_columns}
        else:
            # Include all columns but ensure preferred ones exist for consistency
            row = normalized
            for col in preferred_columns:
                row.setdefault(col, "")

        writer.writerow(row)

    def _build_child_specs(self, endpoint: str, expand_children: list[str]) -> list[dict[str, Any]]:
        """Resolve the requested child collections into output-table specs.

        Each spec carries its own table definition, the parent foreign-key mapping, and the child
        primary key (child's own metadata key(s) + the parent FK column(s)).
        """
        if not expand_children:
            return []

        nav_map = {nav["name"]: nav for nav in self.client.list_navigation_properties(endpoint)}
        # Child tables are prefixed with the parent's effective output-table name so they stay
        # consistent with a custom destination.table_name and two rows expanding the same endpoint
        # into differently-named parents don't collide on the same child table name.
        parent_table = self.config.destination.table_name or endpoint
        parent_keys = self.client.entity_keys(endpoint) or ["id"]
        # FK column name is fixed as "parent_id" for the common single-key case; composite parent
        # keys fall back to one "parent_<key>" column each so the mapping stays unambiguous.
        if len(parent_keys) == 1:
            fk_map = {"parent_id": parent_keys[0]}
        else:
            fk_map = {f"parent_{key}": key for key in parent_keys}
        fk_cols = list(fk_map.keys())

        specs: list[dict[str, Any]] = []
        for nav_name in expand_children:
            nav = nav_map.get(nav_name)
            if nav is None:
                raise UserException(
                    f"'{nav_name}' is not an expandable child collection of endpoint '{endpoint}'. "
                    f"Available line collections: {sorted(nav_map)}."
                )
            own_keys = list(nav.get("keys", []))
            child_pk = list(dict.fromkeys(own_keys + fk_cols))
            table_name = f"{parent_table}_{nav_name}"
            if not own_keys:
                logging.warning(
                    "Child collection '%s' has no resolvable key in metadata; table '%s' will be "
                    "keyed by 'parent_id' alone, which can collapse multiple lines per parent under "
                    "incremental load. Consider a full load for this configuration.",
                    nav_name,
                    table_name,
                )
            child_state = self.state.setdefault("tables", {}).setdefault(table_name, {})
            previous_columns = child_state.get("columns", [])
            initial_columns = list(dict.fromkeys(chain(previous_columns, own_keys, fk_cols)))
            child_table = self.create_out_table_definition(
                table_name,
                incremental=self.config.destination.incremental,
                primary_key=child_pk or None,
                columns=initial_columns or None,
                has_header=True,
            )
            specs.append({
                "nav_name": nav_name,
                "table_name": table_name,
                "table": child_table,
                "fk_map": fk_map,
                "child_pk": child_pk,
                "initial_columns": initial_columns,
                "rows": 0,
                "final_columns": list(initial_columns),
            })
        return specs

    @staticmethod
    def _split_record(
        record: dict[str, Any], child_nav_names: set[str]
    ) -> tuple[dict[str, Any], dict[str, list], set[str]]:
        """Split a record into its parent part, its expanded child collections, and the set of
        children that Business Central truncated.

        With no expanded children, the original record is returned unchanged (backwards compatible).

        Expanded collections carry nested OData annotations keyed as ``<nav>@odata.*`` (e.g.
        ``salesInvoiceLines@odata.nextLink``). These do NOT start with ``@odata.`` so they survive
        _strip_odata_metadata; they must be kept out of the parent table, and a nested
        ``@odata.nextLink`` is the reliable signal that BC returned only part of that child collection.
        """
        if not child_nav_names:
            return record, {}, set()

        parent: dict[str, Any] = {}
        children: dict[str, list] = {}
        truncated: set[str] = set()
        for key, value in record.items():
            if key in child_nav_names:
                if isinstance(value, list):
                    children[key] = value
                elif value:
                    children[key] = [value]
                else:
                    children[key] = []
            elif "@odata." in key:
                # Nested expansion annotation - never belongs in the parent table.
                nav = key.split("@", 1)[0]
                if nav in child_nav_names and key.endswith("@odata.nextLink"):
                    truncated.add(nav)
            else:
                parent[key] = value
        return parent, children, truncated

    def _write_children(
        self,
        child_specs: list[dict[str, Any]],
        child_writers: dict[str, Any],
        parent_part: dict[str, Any],
        children: dict[str, list],
    ) -> None:
        """Write each child collection's rows, injecting the parent foreign key on every row."""
        for spec in child_specs:
            nav_name = spec["nav_name"]
            fk_values = {
                fk_col: self._stringify_value(parent_part.get(parent_key))
                for fk_col, parent_key in spec["fk_map"].items()
            }
            for child_row in children.get(nav_name, []):
                normalized_child = self._normalize_record(child_row)
                normalized_child.update(fk_values)
                child_writers[nav_name].writerow(normalized_child)
                spec["rows"] += 1

    def _finalise_table(self, table, final_columns: list[str], primary_key: list[str] | None) -> None:
        existing_columns = set(getattr(table, "column_names", []) or [])
        for column in final_columns:
            if column not in existing_columns:
                table.add_column(column)
                existing_columns.add(column)
        if primary_key:
            table.primary_key = list(primary_key)
        self.write_manifest(table)

    def _ensure_row_state(self) -> dict[str, Any]:
        return self.state.setdefault("tables", {}).setdefault(self._state_key(), {})

    def _state_key(self) -> str:
        return self.config.destination.table_name or self.config.source.endpoint

    def _validate_column_selection(self, incremental_field: str | None) -> None:
        """Validate that primary key and incremental field are in selected columns if defined."""
        selected_columns = self.config.source.selected_columns
        if not selected_columns:
            return

        selected_set = set(selected_columns)
        primary_key = self.config.destination.primary_key

        missing_columns = []

        if primary_key:
            missing_columns.extend([col for col in primary_key if col not in selected_set])

        if incremental_field and incremental_field not in selected_set:
            missing_columns.append(incremental_field)

        if missing_columns:
            raise UserException(
                f"The following columns are required but not among the selected columns: {missing_columns}. "
                f"Please add them to the column selection."
            )

    def _normalize_record(self, record: dict[str, Any]) -> dict[str, str]:
        """Convert all record values to strings for CSV output."""
        return {key: self._stringify_value(value) for key, value in record.items()}

    def _stringify_value(self, value: Any) -> str:
        """
        Convert any value to a string suitable for CSV output.

        Handles None, datetime objects, dicts/lists (as JSON), and primitives.
        """
        match value:
            case None:
                return ""
            case datetime():
                return value.astimezone(timezone.utc).isoformat()
            case dict() | list():
                return json.dumps(value, ensure_ascii=False)
            case _:
                return str(value)

    @sync_action("testConnection")
    def test_connection(self):
        """Test API connectivity by fetching the list of companies."""
        try:
            self.client.list_companies()
        except DynamicsClientError as exc:
            raise self._wrap_client_error(exc)

    @sync_action("list_environments")
    def list_environments(self):
        """Fetch available environments for UI dropdown."""
        try:
            environments = self.client.list_environments()
        except DynamicsClientError as exc:
            raise self._wrap_client_error(exc)

        return [SelectElement(item.get("name", "")) for item in environments if item.get("name")]

    @sync_action("list_companies")
    def list_companies(self):
        """Fetch available companies for UI dropdown."""
        try:
            companies = self.client.list_companies()
        except DynamicsClientError as exc:
            raise self._wrap_client_error(exc)

        return [SelectElement(value=item["id"], label=item.get("name") or item["id"]) for item in companies]

    @sync_action("list_endpoints")
    def list_endpoints(self):
        """Fetch available API endpoints for UI dropdown."""
        try:
            endpoints = self.client.list_endpoints()
        except DynamicsClientError as exc:
            raise self._wrap_client_error(exc)

        result = []
        for item in endpoints:
            label = item.get("label")
            if label in ENDPOINTS_REQUIRING_FILTERS:
                label += " (filter required)"
            result.append(SelectElement(value=item["name"], label=label))
        return result

    @sync_action("list_columns")
    def list_columns(self):
        """Fetch columns for the selected endpoint."""
        endpoint = self.config.source.endpoint
        if not endpoint:
            raise UserException("Select an endpoint before listing columns.")

        try:
            columns = self.client.list_columns(endpoint)
        except DynamicsClientError as exc:
            raise self._wrap_client_error(exc)

        return [SelectElement(value=col["name"], label=self._column_label(col)) for col in columns]

    @sync_action("list_navigation_properties")
    def list_navigation_properties(self):
        """Fetch the expandable child collections (line items) of the selected endpoint."""
        endpoint = self.config.source.endpoint
        if not endpoint:
            raise UserException("Select an endpoint before listing related line items.")

        try:
            navs = self.client.list_navigation_properties(endpoint)
        except DynamicsClientError as exc:
            raise self._wrap_client_error(exc)

        return [SelectElement(value=nav["name"], label=nav.get("label") or nav["name"]) for nav in navs]

    @staticmethod
    def _column_label(column: dict[str, Any]) -> str:
        """Format column label with optional type annotation."""
        label = column.get("label") or column["name"]
        col_type = column.get("type")
        return f"{label} ({col_type})" if col_type else label

    @staticmethod
    def _wrap_client_error(error: DynamicsClientError) -> UserException:
        """Convert API client errors to user-friendly messages."""
        if isinstance(error, DynamicsAuthenticationError):
            message = f"Authentication failed: {error}"
        elif isinstance(error, DynamicsRateLimitError):
            message = (
                "Dynamics 365 Business Central throttled the request. "
                "Consider lowering page size or scheduling runs less frequently."
            )
        else:
            message = str(error)

        return UserException(message)


if __name__ == "__main__":
    try:
        comp = Component()
        # this triggers the run method by default and is controlled by the configuration.action parameter
        comp.execute_action()
    except UserException as exc:
        logging.exception(exc)
        exit(1)
    except Exception as exc:
        logging.exception(exc)
        exit(2)
