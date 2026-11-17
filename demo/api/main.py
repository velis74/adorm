"""The demo API: the examples and the concurrency scenarios, run on every available driver.

    uv run python demo.py

At startup Django is set up (the drivers read ``DATABASES``), every available driver is loaded with its two
connection profiles (`demo.api.drivers`), and each profile's pool opens a connection. One run, of an example or a
scenario, executes at a time; the SQL the drivers send is recorded during the run (`demo.api.capture`). With
``ADORM_DEMO_FRONTEND`` naming a directory, the built frontend is served from it at ``/``.
"""

from __future__ import annotations

import asyncio
import os
import time

from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Body, FastAPI, HTTPException, Path
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from pydantic_core import to_jsonable_python

import adorm

from adorm.settings import close_pools
from demo.api import drivers
from demo.api.capture import CAPTURE
from demo.api.examples import Example, EXAMPLES
from demo.api.scenarios import describe, run_scenario, SCENARIOS
from demo.models.library import Book

FRONTEND_ENV = "ADORM_DEMO_FRONTEND"

EXAMPLE_RUNS = {
    "example": "search_books",
    "params": {"search": "karamazov", "genre": ""},
    "runs": [
        {
            "driver": "django",
            "ms": 5.1,
            "result": [{"id": 10, "title": "The Brothers Karamazov", "genre": "novel", "year": 1880}],
            "error": None,
            "statements": [
                {
                    "session": "example",
                    "sql": 'SELECT DISTINCT "library_book"."id" AS "id", ...',
                    "params": ["%karamazov%", "%karamazov%"],
                    "at_ms": 2.4,
                    "duration_ms": 1.95,
                    "error": None,
                }
            ],
        }
    ],
}
SCENARIO_RUNS = {
    "scenario": "lock_timeout",
    "runs": [
        {
            "id": "lock_timeout",
            "driver": "asyncpg",
            "ms": 512.3,
            "flows": ["A", "B"],
            "outcomes": {
                "A": {"status": "ok", "result": "held"},
                "B": {
                    "status": "error",
                    "error": "OperationalError",
                    "sqlstate": "55P03",
                    "message": "canceling statement due to lock timeout",
                    "held_sessions": None,
                },
            },
            "events": [
                {"at_ms": 1.2, "flow": "A", "kind": "info", "text": "has the row lock"},
                {"at_ms": 510.8, "flow": "B", "kind": "error", "text": "OperationalError (SQLSTATE 55P03): ..."},
            ],
            "statements": [
                {
                    "session": "B",
                    "sql": "SET LOCAL lock_timeout = 500",
                    "params": [],
                    "at_ms": 3.0,
                    "duration_ms": 0.2,
                    "error": None,
                }
            ],
        }
    ],
}
"""Response examples of the run endpoints."""

RUN_LOCK = asyncio.Lock()
"""Held for the duration of one run, so that the statements recorded belong to it."""


class Health(BaseModel):
    """State of the demo API."""

    status: Literal["ok"] = Field(description="`ok` once the API has started.")
    adorm_version: str = Field(description="Version of the adorm package.")
    drivers: list[str] = Field(description="The drivers the examples and scenarios run on, in this order.")


class DriverInfo(BaseModel):
    """A driver the demo knows."""

    name: str = Field(description="The driver's name and the name of its connection profile.")
    description: str = Field(description="How the driver executes operations.")
    available: bool = Field(description="Whether the driver's module imports.")
    reason: str | None = Field(None, description="Why the driver is not available; `null` when it is.")
    mode: Literal["threaded", "async"] | None = Field(None, description="The driver's execution mode.")


class ParamInfo(BaseModel):
    """A parameter of an example."""

    name: str = Field(description="The key in the `params` of a run.")
    label: str = Field(description="What the parameter means.")
    kind: Literal["text", "int", "choice"] = Field(description="The value's type; `choice` takes one of `choices`.")
    default: Any = Field(description="The value when a run does not give one.")
    choices: list[Any] = Field(description="The values a `choice` parameter takes; empty for the other kinds.")
    minimum: int | None = Field(description="The smallest value of an `int` parameter; `null` when unbounded.")
    maximum: int | None = Field(description="The largest value of an `int` parameter; `null` when unbounded.")


class ExampleInfo(BaseModel):
    """An example the demo runs on every driver."""

    id: str = Field(description="The example's id.")
    title: str = Field(description="The example's title.")
    description: str = Field(description="What the example shows.")
    code: str = Field(description="The example's source: a coroutine function on the generated models.")
    params: list[ParamInfo] = Field(description="The example's parameters, in the order of its arguments.")
    writes: bool = Field(description="Whether the example writes; the demo reverts the writes after each run.")


class StatementOut(BaseModel):
    """An SQL statement a driver sent to PostgreSQL."""

    session: str | None = Field(description="The session that held the connection; `null` outside a lease.")
    sql: str = Field(description="The statement as the database received it, with its placeholders.")
    params: list[Any] = Field(description="The statement's parameters.")
    at_ms: float = Field(description="When it started, in milliseconds since the run started.")
    duration_ms: float = Field(description="How long it took, in milliseconds.")
    error: str | None = Field(description="The error it raised, `null` when it succeeded.")


class ErrorOut(BaseModel):
    """An exception a run or a flow raised."""

    status: Literal["error"] = Field("error", description="Always `error`.")
    error: str = Field(description="The exception's class name.")
    sqlstate: str | None = Field(description="The SQLSTATE of a database error; `null` for other errors.")
    message: str = Field(description="The first line of the exception's message.")
    held_sessions: list[str] | None = Field(
        description="The names of the sessions whose slots the flow held, for a `DeadlockError`; `null` otherwise."
    )


class FlowOk(BaseModel):
    """A flow that returned."""

    status: Literal["ok"] = Field(description="Always `ok`.")
    result: Any = Field(description="What the flow returned, as JSON.")


class FlowCancelled(BaseModel):
    """A flow whose task was cancelled."""

    status: Literal["cancelled"] = Field(description="Always `cancelled`.")
    statement: str = Field(
        description="The `adorm.CancellationOutcome` of the statement the flow ran when it was cancelled."
    )


FlowOutcome = Annotated[FlowOk | ErrorOut | FlowCancelled, Field(discriminator="status")]


class ExampleRun(BaseModel):
    """An example's run on one driver."""

    driver: str = Field(description="The driver the example ran on.")
    ms: float = Field(description="Wall time of the example's coroutine, in milliseconds.")
    result: Any = Field(description="What the example returned, as JSON; `null` when it raised.")
    error: ErrorOut | None = Field(description="What it raised; `null` when it returned.")
    statements: list[StatementOut] = Field(description="The SQL the example sent, in the order it started.")


class ExampleRuns(BaseModel):
    """An example's runs, one per available driver, in the order of `/api/drivers`."""

    model_config = ConfigDict(json_schema_extra={"examples": [EXAMPLE_RUNS]})

    example: str = Field(description="The example's id.")
    params: dict[str, Any] = Field(description="The arguments the example ran with.")
    runs: list[ExampleRun] = Field(description="One run per available driver.")


class RunRequest(BaseModel):
    """The arguments of an example run; a parameter left out takes its default."""

    params: dict[str, Any] = Field(default_factory=dict, examples=[{"search": "war", "genre": "novel"}])


class ScenarioInfo(BaseModel):
    """A concurrency scenario."""

    id: str = Field(description="The scenario's id.")
    title: str = Field(description="The scenario's title.")
    description: str = Field(description="What the flows do and how they end.")
    code: str = Field(description="The source of the flows the scenario runs.")
    choreography: str = Field(description="The source of the function that starts and steers the flows.")


class FlowEvent(BaseModel):
    """Something a flow saw, or the observer (`demo`) noted."""

    at_ms: float = Field(description="Milliseconds since the run started.")
    flow: str = Field(description="The flow (`A`, `B` …), or `demo` for an observation.")
    kind: Literal["info", "ok", "error", "observe"] = Field(
        description="`info` for a step, `ok` for a flow's last step, `error` for what a flow raised, `observe` for "
        "an observation."
    )
    text: str = Field(description="What the flow saw or the observer noted.")


class ScenarioRun(BaseModel):
    """A scenario's run on one driver."""

    id: str = Field(description="The scenario's id.")
    driver: str = Field(description="The driver the scenario ran on.")
    ms: float = Field(description="Wall time of the run, in milliseconds.")
    flows: list[str] = Field(description="The flows, sorted by name.")
    outcomes: dict[str, FlowOutcome] = Field(description="Each flow's outcome, by flow.")
    events: list[FlowEvent] = Field(description="What every flow saw, in the order it happened.")
    statements: list[StatementOut] = Field(description="The SQL of all flows, in the order it started.")


class ScenarioRuns(BaseModel):
    """A scenario's runs, one per available driver."""

    model_config = ConfigDict(json_schema_extra={"examples": [SCENARIO_RUNS]})

    scenario: str = Field(description="The scenario's id.")
    runs: list[ScenarioRun] = Field(description="One run per available driver.")


async def warm_up(names: list[str]) -> None:
    """Open a connection in every profile's pool."""
    for name in names:
        with adorm.create_session(name, "warm-up").activate():
            await Book.objects.count()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Sets Django up, loads the available drivers and registers their profiles; closes the pools at shutdown."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo.backend.settings")
    import django

    await asyncio.to_thread(django.setup)
    CAPTURE.watch_django()
    loaded = {driver.name: await asyncio.to_thread(driver.load) for driver in drivers.available()}
    app.state.drivers = list(loaded)
    profiles = {}
    for driver in drivers.available():
        profiles.update(driver.profiles(loaded[driver.name]))
    adorm.configure(profiles=profiles)
    try:
        await warm_up(list(profiles))
        yield
    finally:
        await close_pools()
        adorm.configure()


app = FastAPI(
    title="adorm demo",
    version=adorm.__version__,
    description="A library's books, editions and loans on adorm: examples and concurrency scenarios, each run on "
    "every available driver with the SQL it produced.",
    lifespan=lifespan,
)

NOT_FOUND = {404: {"description": "No example or scenario has this id."}}


def available_drivers() -> list[str]:
    names = app.state.drivers
    if not names:
        raise HTTPException(503, "no driver is available")
    return names


@app.get("/api/health", tags=["demo"], summary="State of the API")
async def health() -> Health:
    """The adorm version and the drivers the examples and scenarios run on."""
    return Health(status="ok", adorm_version=adorm.__version__, drivers=app.state.drivers)


@app.get("/api/drivers", tags=["demo"], summary="The drivers the demo knows")
async def list_drivers() -> list[DriverInfo]:
    """Every driver of the demo, with whether it is available; the examples and scenarios run on the available ones."""
    out = []
    for driver in drivers.DRIVERS:
        reason = driver.unavailable_reason()
        mode = None if reason is not None else driver.load().mode
        out.append(
            DriverInfo(
                name=driver.name, description=driver.description, available=reason is None, reason=reason, mode=mode
            )
        )
    return out


@app.get("/api/examples", tags=["examples"], summary="The examples")
async def list_examples() -> list[ExampleInfo]:
    """Every example with its source and parameters."""
    return [ExampleInfo(**example.as_dict()) for example in EXAMPLES.values()]


async def run_example(example: Example, driver: str, arguments: dict[str, Any]) -> ExampleRun:
    """Run ``example`` on ``driver``'s profile in the session ``(driver, "example")``, then its ``undo``."""
    with adorm.create_session(driver, "example").activate():
        CAPTURE.start()
        started = time.monotonic()
        result = error = None
        try:
            result = await example.function(**arguments)
        except Exception as exc:
            error = ErrorOut(**describe(exc))
        elapsed = time.monotonic() - started
        statements = await CAPTURE.stop()
        if example.undo is not None and error is None:
            await example.undo(result)
    return ExampleRun(
        driver=driver,
        ms=round(elapsed * 1000, 1),
        result=to_jsonable_python(result),
        error=error,
        statements=[StatementOut(**statement.as_dict()) for statement in statements],
    )


@app.post(
    "/api/examples/{example_id}/run",
    tags=["examples"],
    summary="Run an example on every driver",
    responses={
        **NOT_FOUND,
        422: {"description": "A parameter is unknown or its value does not fit."},
        503: {"description": "No driver is available."},
    },
)
async def post_example_run(
    example_id: Annotated[str, Path(description="The example's id, from `/api/examples`.")],
    request: Annotated[RunRequest | None, Body()] = None,
) -> ExampleRuns:
    """Runs the example on each available driver in turn and answers, per driver, what it returned or raised, the
    SQL it sent and the time it took. Writes are reverted after each driver's run."""
    example = EXAMPLES.get(example_id)
    if example is None:
        raise HTTPException(404, f"no example {example_id!r}")
    try:
        arguments = example.arguments(request.params if request is not None else {})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    names = available_drivers()
    async with RUN_LOCK:
        runs = [await run_example(example, name, arguments) for name in names]
    return ExampleRuns(example=example.id, params=to_jsonable_python(arguments), runs=runs)


@app.get("/api/scenarios", tags=["scenarios"], summary="The concurrency scenarios")
async def list_scenarios() -> list[ScenarioInfo]:
    """Every concurrency scenario with the source of its flows and of the function that steers them."""
    return [ScenarioInfo(**scenario.as_dict()) for scenario in SCENARIOS.values()]


@app.post(
    "/api/scenarios/{scenario_id}/run",
    tags=["scenarios"],
    summary="Run a scenario on every driver",
    responses={**NOT_FOUND, 503: {"description": "No driver is available."}},
)
async def post_scenario_run(
    scenario_id: Annotated[str, Path(description="The scenario's id, from `/api/scenarios`.")],
) -> ScenarioRuns:
    """Runs the scenario on each available driver in turn and answers, per driver, what every flow saw and when, the
    SQL of every flow and each flow's outcome. The data set is restored after each run."""
    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        raise HTTPException(404, f"no scenario {scenario_id!r}")
    names = available_drivers()
    async with RUN_LOCK:
        runs = [ScenarioRun(**await run_scenario(scenario, name)) for name in names]
    return ScenarioRuns(scenario=scenario.id, runs=runs)


if os.environ.get(FRONTEND_ENV):
    app.mount("/", StaticFiles(directory=os.environ[FRONTEND_ENV], html=True), name="frontend")
