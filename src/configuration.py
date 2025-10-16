from enum import Enum

from keboola.component.exceptions import UserException
from pydantic import BaseModel, Field, ValidationError, computed_field


class LoadType(str, Enum):
    full_load = "full_load"
    incremental_load = "incremental_load"


class Connection(BaseModel):
    tenant_id: str
    environment: str = ""
    company_id: str = ""


class Source(BaseModel):
    endpoint: str = ""
    selected_columns: list[str] = Field(default_factory=list)
    filter_expression: str = ""
    incremental_field: str = ""
    initial_since: str = ""


class Destination(BaseModel):
    table_name: str = ""
    load_type: LoadType = Field(default=LoadType.incremental_load)
    primary_key: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def incremental(self) -> bool:
        return self.load_type == LoadType.incremental_load


class Configuration(BaseModel):
    connection: Connection
    source: Source = Field(default_factory=Source)
    destination: Destination = Field(default_factory=Destination)
    debug: bool = False

    def __init__(self, **data):
        try:
            super().__init__(**data)
        except ValidationError as e:
            error_messages = [f"{err['loc'][0]}: {err['msg']}" for err in e.errors()]
            raise UserException(f"Validation Error: {', '.join(error_messages)}")
