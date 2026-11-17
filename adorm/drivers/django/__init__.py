"""The Django ORM driver.

`DjangoDriver` executes adorm reads and writes with Django's synchronous ORM on a thread per connection;
`adorm.drivers.django.translation` turns adorm queries into Django query sets; `adorm.drivers.django.vendors` holds
what differs per database backend. Importing this package imports Django.
"""

from adorm.drivers.django.driver import (
    adopt_context_locals,
    backend_pid,
    CAPABILITIES,
    clear_content_types_on_migrate,
    connection_defaults,
    database_key,
    DjangoConnection,
    DjangoDriver,
    DjangoMonitor,
    map_django_error,
    model_row,
    notice_event,
    profile_vendor,
    PSYCOPG2_CAPABILITIES,
    READ_CAPABILITIES,
    wrapper_vendor,
)
from adorm.drivers.django.schema import expected_tables, model_schema_hash

__all__ = [
    "CAPABILITIES",
    "PSYCOPG2_CAPABILITIES",
    "READ_CAPABILITIES",
    "DjangoConnection",
    "DjangoDriver",
    "DjangoMonitor",
    "adopt_context_locals",
    "backend_pid",
    "clear_content_types_on_migrate",
    "connection_defaults",
    "database_key",
    "expected_tables",
    "map_django_error",
    "model_row",
    "model_schema_hash",
    "notice_event",
    "profile_vendor",
    "wrapper_vendor",
]
