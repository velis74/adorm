"""The native PostgreSQL driver on asyncpg.

`AsyncpgDriver` executes adorm operations on asyncpg connections, on the event loop without threads;
`adorm.drivers.asyncpg.compiler` compiles Django queries into SQL for asyncpg, without a database connection.
Importing this package imports asyncpg, Django and psycopg 3.
"""

from adorm.drivers.asyncpg.driver import (
    AsyncpgConnection,
    AsyncpgDriver,
    CAPABILITIES,
    connect_arguments,
    DSN_PARAMETERS,
    IN_BULK_BATCH_SIZE,
    ISOLATION_LEVELS,
    MAX_GET_RESULTS,
    server_version_num,
)

__all__ = [
    "CAPABILITIES",
    "DSN_PARAMETERS",
    "IN_BULK_BATCH_SIZE",
    "ISOLATION_LEVELS",
    "MAX_GET_RESULTS",
    "AsyncpgConnection",
    "AsyncpgDriver",
    "connect_arguments",
    "server_version_num",
]
