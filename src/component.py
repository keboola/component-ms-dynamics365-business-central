import json
import logging
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

        logging.info("Starting extraction for endpoint '%s'.", self.config.source.endpoint)

        iterator = self.client.iterate_endpoint(
            self.config.source.endpoint,
            include_company_scope=True,
            selected_columns=self.config.source.selected_columns,
            filter_expression=self.config.source.filter_expression or None,
            incremental_field=incremental_field,
            incremental_value=incremental_value,
            custom_url_suffix=self.config.source.custom_url_suffix or None,
        )
        records_iter = iter(iterator)
        first_record = next(records_iter, None)

        first_record_keys = list(first_record.keys()) if first_record else []
        preferred_columns = self.config.source.selected_columns or []
        base_columns = list(dict.fromkeys(chain(previous_columns, preferred_columns, first_record_keys)))

        table = self.create_out_table_definition(
            self.config.destination.table_name or self.config.source.endpoint,
            incremental=self.config.destination.incremental,
            primary_key=self.config.destination.primary_key or None,
            columns=base_columns or None,
            has_header=True,
        )

        total_rows = 0
        has_custom_selection = bool(self.config.source.selected_columns)
        record_stream = chain([first_record], records_iter) if first_record else records_iter

        with ElasticDictWriter(table.full_path, list(base_columns)) as writer:
            if writer.fieldnames:
                writer.writeheader()

            for record in record_stream:
                self._process_record(
                    writer,
                    record,
                    preferred_columns,
                    has_custom_selection,
                )
                total_rows += 1

            final_columns = list(writer.fieldnames) if writer.fieldnames else []

        self._finalise_table(self.config, table, final_columns)

        if total_rows == 0:
            logging.info("No records returned for endpoint '%s'. Output file left empty.", self.config.source.endpoint)
        else:
            logging.info("Finished endpoint '%s'. Rows written: %s.", self.config.source.endpoint, total_rows)

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

    def _finalise_table(self, config: Configuration, table, final_columns: list[str]) -> None:
        existing_columns = set(getattr(table, "column_names", []) or [])
        for column in final_columns:
            if column not in existing_columns:
                table.add_column(column)
                existing_columns.add(column)
        if config.destination.primary_key:
            table.primary_key = list(config.destination.primary_key)
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
