"""Drivers execute adorm operations on an ORM or a database library.

This package holds the driver contract. A bundled driver lives in a subpackage that is imported only when a
connection profile names it, so importing ``adorm`` imports no ORM.
"""

from adorm.drivers.base import (
    BackendActivity,
    Capabilities,
    check_channel,
    DatabaseMonitor,
    Driver,
    DriverConnection,
    ExecutionMode,
    is_connection_error,
    is_custom_option,
    is_refusal,
    Operation,
    operation_models,
    Profile,
    ThreadedConnection,
    ThreadedDriver,
)

__all__ = [
    "BackendActivity",
    "Capabilities",
    "DatabaseMonitor",
    "Driver",
    "DriverConnection",
    "ExecutionMode",
    "Operation",
    "Profile",
    "ThreadedConnection",
    "ThreadedDriver",
    "check_channel",
    "is_connection_error",
    "is_custom_option",
    "is_refusal",
    "operation_models",
]
