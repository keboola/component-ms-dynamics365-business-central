import logging
import re
from collections.abc import Iterator
from typing import Any
from xml.etree import ElementTree

import requests
from requests import Response
from requests.exceptions import RequestException

from configuration import Configuration

# API Configuration
API_VERSION = "v2.0"

BASE_URL = "https://api.businesscentral.dynamics.com"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

OAUTH_SCOPE = "https://api.businesscentral.dynamics.com/.default offline_access"
PAGE_SIZE = 2000
DEFAULT_TIMEOUT = 60

# OData XML namespaces for metadata parsing
ODATA_NS = {
    "edmx": "http://docs.oasis-open.org/odata/ns/edmx",
    "edm": "http://docs.oasis-open.org/odata/ns/edm",
}


class DynamicsClientError(Exception):
    """Base exception for Dynamics 365 Business Central API errors."""


class DynamicsAuthenticationError(DynamicsClientError):
    """Authentication or authorization failure."""


class DynamicsRateLimitError(DynamicsClientError):
    """API throttling/rate limit error."""


class DynamicsClient:
    """
    Business Central API client for ETL operations.

    Handles:
    - OAuth token management with automatic refresh
    - OData metadata parsing for schema discovery
    - Paginated data extraction with streaming
    """

    def __init__(
        self,
        configuration: Configuration,
        oauth_credentials,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.config = configuration
        self.app_key = oauth_credentials.appKey
        self.app_secret = oauth_credentials.appSecret

        # Extract OAuth tokens from state or credentials
        tokens_from_state = (state or {}).get("oauth", {})
        oauth_data = getattr(oauth_credentials, "data", {}) or {}
        self.access_token: str | None = tokens_from_state.get("#access_token") or oauth_data.get("access_token")
        self.refresh_token: str | None = tokens_from_state.get("#refresh_token") or oauth_data.get("refresh_token")

        self.session = requests.Session()
        self._metadata_cache: dict[str, Any] | None = None
        self._tokens_changed = False

    @property
    def tokens_changed(self) -> bool:
        """Returns True if OAuth tokens were refreshed during this session."""
        return self._tokens_changed

    @property
    def oauth_state(self) -> dict[str, str | None]:
        """Returns current OAuth tokens for state persistence."""
        return {"#access_token": self.access_token, "#refresh_token": self.refresh_token}

    def list_environments(self) -> list[dict[str, Any]]:
        """Fetch available environments from the Business Central instance."""
        url = "https://api.businesscentral.dynamics.com/environments/v1.1"
        response = self._request("GET", url)

        # API returns {"value": [...]} structure
        data = response.json()
        return data.get("value", [])

    def list_companies(self) -> list[dict[str, Any]]:
        """Fetch available companies from the Business Central instance."""
        url = self._build_url("companies")
        response = self._request("GET", url, params={"$select": "id,name,displayName"})
        return response.json().get("value", [])

    def list_endpoints(self) -> list[dict[str, str]]:
        """Fetch available API endpoints with human-readable labels."""
        metadata = self._get_metadata()
        entity_sets = metadata.get("entity_sets", {})

        endpoints = [
            {
                "name": name,
                "label": info.get("label") or _humanize_label(name),
                "entity_type": info.get("entity_type"),
            }
            for name, info in entity_sets.items()
        ]
        return sorted(endpoints, key=lambda e: e["label"].lower())

    def list_columns(self, endpoint: str) -> list[dict[str, str]]:
        """Fetch column schema for a specific endpoint."""
        metadata = self._get_metadata()
        entity = metadata.get("entity_sets", {}).get(endpoint)

        if not entity:
            raise DynamicsClientError(f"Endpoint '{endpoint}' not found in metadata.")

        properties = entity.get("properties", [])
        keys = set(entity.get("keys", []))

        columns = [
            {
                "name": prop["name"],
                "type": prop.get("type", ""),
                "label": _humanize_label(prop["name"]),
                "is_key": prop["name"] in keys,
            }
            for prop in properties
            if prop.get("name")
        ]
        return sorted(columns, key=lambda c: c["name"].lower())

    def iterate_endpoint(
        self,
        endpoint: str,
        *,
        include_company_scope: bool = True,
        selected_columns: list[str] | None = None,
        filter_expression: str | None = None,
        incremental_field: str | None = None,
        incremental_value: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """
        Stream records from an endpoint with automatic pagination.

        Yields clean records (without OData metadata) until all pages are consumed.
        """
        next_link: str | None = None

        while True:
            rows, next_link = self._fetch_page(
                endpoint,
                include_company_scope=include_company_scope,
                selected_columns=selected_columns,
                filter_expression=filter_expression,
                incremental_field=incremental_field,
                incremental_value=incremental_value,
                next_link=next_link,
            )

            for row in rows:
                yield _strip_odata_metadata(row)

            if not next_link:
                break

    def _fetch_page(
        self,
        endpoint: str,
        *,
        include_company_scope: bool,
        selected_columns: list[str] | None,
        filter_expression: str | None,
        incremental_field: str | None,
        incremental_value: str | None,
        next_link: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Fetch a single page of data, either from next_link or by building the query."""
        if next_link:
            # Pagination links are already absolute URLs
            response = self._request("GET", next_link)
        else:
            url = self._build_url(endpoint, include_company_scope=include_company_scope)
            params = self._build_query_params(
                selected_columns=selected_columns,
                filter_expression=filter_expression,
                incremental_field=incremental_field,
                incremental_value=incremental_value,
            )
            response = self._request("GET", url, params=params)

        data = response.json()
        return data.get("value", []), data.get("@odata.nextLink")

    def _build_url(self, endpoint: str, include_company_scope: bool = False) -> str:
        """
        Build complete API URL for an endpoint.

        Args:
            endpoint: Endpoint name (e.g., 'companies', 'itemLedgerEntries')
            include_company_scope: If True, include companies({company_id}) in path

        Returns:
            Complete URL ready to use
        """
        tenant_id = self.config.connection.tenant_id
        environment = self.config.connection.environment
        company_id = self.config.connection.company_id

        # Clean endpoint path
        endpoint = endpoint.lstrip("/")

        # Replace placeholders
        for placeholder in ("{companyId}", "{company_id}"):
            endpoint = endpoint.replace(placeholder, company_id)

        # Remove any existing company scope prefix
        if endpoint.lower().startswith("companies("):
            match = re.match(r"companies\([^)]+\)/(.+)", endpoint, re.IGNORECASE)
            if match:
                endpoint = match.group(1)

        # Build URL parts
        parts = [BASE_URL, API_VERSION, tenant_id]
        if environment:
            parts.append(environment)
        parts.extend(["api", API_VERSION])

        # Add company scope if needed
        if include_company_scope:
            parts.append(f"companies({company_id})")

        # Add endpoint
        parts.append(endpoint)

        return "/".join(parts)

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        retry: bool = True,
    ) -> Response:
        """
        Execute an HTTP request with automatic OAuth refresh on 401.

        Args:
            method: HTTP method (GET, POST, etc.)
            url: Complete URL (built by _build_url or pagination link)
            params: Query parameters
            retry: Whether to retry once on 401 after token refresh
        """
        headers = self._build_auth_headers()

        logging.debug("%s %s params=%s", method, url, params)

        try:
            response = self.session.request(
                method,
                url,
                params=params,
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
            )
        except RequestException as exc:
            raise DynamicsClientError(f"Request failed: {exc}") from exc

        # Handle 401 by refreshing token and retrying once
        if response.status_code == 401 and retry:
            logging.info("Access token expired, refreshing...")
            self._refresh_oauth_token()
            return self._request(method, url, params=params, retry=False)

        # Handle rate limiting
        if response.status_code == 429:
            raise DynamicsRateLimitError("API throttled the request (HTTP 429).")

        # Handle other errors
        if response.status_code >= 400:
            self._raise_for_status(response, url)

        return response

    def _build_auth_headers(self) -> dict[str, str]:
        """Build HTTP headers with OAuth bearer token."""
        if not self.access_token:
            raise DynamicsAuthenticationError("Missing OAuth access token.")

        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _refresh_oauth_token(self) -> None:
        """Refresh the OAuth access token using the refresh token."""
        if not self.refresh_token:
            raise DynamicsAuthenticationError("Refresh token not available.")

        payload = {
            "client_id": self.app_key,
            "client_secret": self.app_secret,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "scope": OAUTH_SCOPE,
        }

        try:
            response = self.session.post(TOKEN_URL, data=payload, timeout=DEFAULT_TIMEOUT)
        except RequestException as exc:
            raise DynamicsAuthenticationError(f"Token refresh request failed: {exc}") from exc

        if response.status_code >= 400:
            try:
                error_data = response.json()
                error_msg = error_data.get("error_description") or error_data.get("error") or response.text
            except ValueError:
                error_msg = response.text
            raise DynamicsAuthenticationError(f"Token refresh failed ({response.status_code}): {error_msg}")

        try:
            data = response.json()
        except ValueError as exc:
            raise DynamicsAuthenticationError("Invalid JSON in token refresh response.") from exc

        new_access_token = data.get("access_token")
        if not new_access_token:
            raise DynamicsAuthenticationError("Token refresh response missing access_token.")

        # Update tokens and track changes
        tokens_updated = False
        if new_access_token != self.access_token:
            self.access_token = new_access_token
            tokens_updated = True

        new_refresh_token = data.get("refresh_token")
        if new_refresh_token and new_refresh_token != self.refresh_token:
            self.refresh_token = new_refresh_token
            tokens_updated = True

        self._tokens_changed = self._tokens_changed or tokens_updated

    def _raise_for_status(self, response: Response, url: str) -> None:
        """Parse API error response and raise appropriate exception."""
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        message = (
            payload.get("error", {}).get("message")
            or payload.get("message")
            or response.text
            or f"HTTP {response.status_code}"
        )

        if response.status_code in (401, 403):
            raise DynamicsAuthenticationError(f"Authentication failed: {message}")

        raise DynamicsClientError(f"API error {response.status_code} at {url}: {message}")

    def _get_metadata(self) -> dict[str, Any]:
        """Fetch and cache the OData metadata document."""
        if self._metadata_cache is not None:
            return self._metadata_cache

        response = self._request("GET", "$metadata")
        self._metadata_cache = _parse_odata_metadata(response.text)
        return self._metadata_cache

    def _build_query_params(
        self,
        *,
        selected_columns: list[str] | None,
        filter_expression: str | None,
        incremental_field: str | None,
        incremental_value: str | None,
    ) -> dict[str, Any]:
        """Build OData query parameters for $select, $filter, and $top."""
        params: dict[str, Any] = {"$top": PAGE_SIZE}

        # Add $select if specific columns are requested
        if selected_columns:
            clean_columns = [col.strip() for col in selected_columns if col and col.strip()]
            if clean_columns:
                params["$select"] = ",".join(sorted(set(clean_columns)))

        # Build $filter from custom expression and/or incremental field
        filters = []
        if filter_expression:
            filters.append(f"({filter_expression})")
        if incremental_field and incremental_value:
            filters.append(f"{incremental_field} gt {_format_filter_value(incremental_value)}")

        if filters:
            params["$filter"] = " and ".join(filters)

        return params


def _parse_odata_metadata(xml_body: str) -> dict[str, Any]:
    """
    Parse OData $metadata XML into a structured dictionary.

    Returns:
        {
            "entity_sets": {
                "endpoint_name": {
                    "entity_type": "Namespace.EntityType",
                    "properties": [{"name": "...", "type": "..."}],
                    "keys": ["id"],
                    "label": "Endpoint Name"
                }
            },
            "entity_types": {...}
        }
    """
    try:
        root = ElementTree.fromstring(xml_body)
    except ElementTree.ParseError as exc:
        raise DynamicsClientError(f"Failed to parse metadata XML: {exc}") from exc

    entity_types: dict[str, dict[str, Any]] = {}
    entity_sets: dict[str, dict[str, Any]] = {}

    # Parse all schemas
    schemas = root.findall("edmx:DataServices/edm:Schema", namespaces=ODATA_NS)
    for schema in schemas:
        namespace = schema.attrib.get("Namespace", "")

        # Parse entity types (data models)
        for entity_type in schema.findall("edm:EntityType", namespaces=ODATA_NS):
            name = entity_type.attrib.get("Name")
            full_name = f"{namespace}.{name}" if namespace else name
            if not full_name:
                continue

            properties = [
                {"name": prop.attrib.get("Name"), "type": prop.attrib.get("Type")}
                for prop in entity_type.findall("edm:Property", namespaces=ODATA_NS)
            ]

            keys = [
                key.attrib.get("Name")
                for key in entity_type.findall("edm:Key/edm:PropertyRef", namespaces=ODATA_NS)
                if key.attrib.get("Name")
            ]

            entity_types[full_name] = {"properties": properties, "keys": keys}

        # Parse entity sets (API endpoints)
        for container in schema.findall("edm:EntityContainer", namespaces=ODATA_NS):
            for entity_set in container.findall("edm:EntitySet", namespaces=ODATA_NS):
                name = entity_set.attrib.get("Name")
                entity_type_name = entity_set.attrib.get("EntityType")

                if not name or not entity_type_name:
                    continue

                entity_info = entity_types.get(entity_type_name, {})
                entity_sets[name] = {
                    "entity_type": entity_type_name,
                    "properties": entity_info.get("properties", []),
                    "keys": entity_info.get("keys", []),
                    "label": _humanize_label(name),
                }

    return {"entity_sets": entity_sets, "entity_types": entity_types}


def _format_filter_value(value: str) -> str:
    """
    Format a value for use in OData $filter expressions.

    Handles:
    - Datetime literals (DATETIME'...')
    - ISO 8601 dates/datetimes
    - Regular strings (wrapped in quotes with escaped quotes)
    """
    # Escape single quotes
    formatted = value.replace("'", "''")

    # Return datetime literals and ISO dates as-is
    if formatted.upper().startswith("DATETIME"):
        return formatted
    if re.match(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?)?$", formatted):
        return formatted

    # Wrap strings in quotes
    return f"'{formatted}'"


def _strip_odata_metadata(record: dict[str, Any]) -> dict[str, Any]:
    """Remove OData control fields (@odata.*) from a record."""
    return {key: value for key, value in record.items() if not key.startswith("@odata.")}


def _humanize_label(value: str) -> str:
    """
    Convert camelCase or snake_case to a human-readable label.

    Examples:
        "customerList" -> "Customer list"
        "sales_orders" -> "Sales orders"
    """
    if not value:
        return value

    # Insert spaces before capital letters
    spaced = re.sub(r"(?<!^)(?<![A-Z])(?=[A-Z])", " ", value)
    # Replace underscores with spaces
    label = spaced.replace("_", " ").strip()

    return label.capitalize()
