"""A list field for every database: PostgreSQL's array, JSON text elsewhere."""

import json

from django.contrib.postgres.fields import ArrayField


class ListField(ArrayField):
    """``ArrayField`` on PostgreSQL; on other databases a ``text`` column holding the list as JSON. Array lookups
    (``overlap``, ``contains`` …) work on PostgreSQL only."""

    def get_internal_type(self) -> str:
        return "ArrayField"

    def db_type(self, connection):
        if connection.vendor == "postgresql":
            return super().db_type(connection)
        return "text"

    def cast_db_type(self, connection):
        if connection.vendor == "postgresql":
            return super().cast_db_type(connection)
        return "text"

    def get_placeholder_sql(self, value, compiler, connection):
        if connection.vendor == "postgresql":
            return super().get_placeholder_sql(value, compiler, connection)
        if hasattr(value, "as_sql"):
            return compiler.compile(value)
        return "%s", (value,)

    def get_placeholder(self, value, compiler, connection):
        if connection.vendor == "postgresql":
            return super().get_placeholder(value, compiler, connection)
        return "%s"

    def get_db_prep_value(self, value, connection, prepared=False):
        if connection.vendor == "postgresql":
            return super().get_db_prep_value(value, connection, prepared)
        return None if value is None else json.dumps(list(value))

    def get_db_prep_save(self, value, connection):
        if connection.vendor == "postgresql":
            return super().get_db_prep_save(value, connection)
        return self.get_db_prep_value(value, connection)

    def from_db_value(self, value, expression, connection):  # noqa: ARG002 - Django's converter signature
        if isinstance(value, str) and connection.vendor != "postgresql":
            return json.loads(value)
        return value
