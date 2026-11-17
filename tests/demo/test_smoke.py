"""The demo end to end: ``demo.py`` sets up the database ``<test database>_demo`` on the test server, then serves the
API in a child process, and every endpoint runs on every available driver.

The database is dropped afterwards. ``ADORM_DEMO_REQUIRED_DRIVERS`` (comma-separated names) fails the suite when a
listed driver is not available.
"""

import json
import os
import socket
import subprocess
import sys
import time

from pathlib import Path

import httpx
import pytest

from demo.database import drop_database, with_database
from tests.djangoproject.settings import DEMO_DATABASE, TEST_DATABASE_URL

ROOT = Path(__file__).parents[2]
DATABASE = DEMO_DATABASE
URL = with_database(TEST_DATABASE_URL, DATABASE)
REQUIRED = [name.strip() for name in os.environ.get("ADORM_DEMO_REQUIRED_DRIVERS", "").split(",") if name.strip()]
REPEATS = 3
"""Runs of each scenario per test: a choreography that only sometimes takes its course fails one of them."""


def demo(*args: str, **kwargs) -> subprocess.CompletedProcess:
    environ = {**os.environ, "ADORM_DEMO_DATABASE_URL": URL}
    environ.pop("DJANGO_SETTINGS_MODULE", None)
    return subprocess.run(  # noqa: S603 - the interpreter running the suite
        [sys.executable, "demo.py", *args], cwd=ROOT, env=environ, capture_output=True, text=True, check=False, **kwargs
    )


def demo_module(module: str, url: str) -> subprocess.CompletedProcess:
    """Run ``python -m module`` on the demo database ``url``."""
    environ = {**os.environ, "ADORM_DEMO_DATABASE_URL": url}
    environ.pop("DJANGO_SETTINGS_MODULE", None)
    return subprocess.run(  # noqa: S603 - the interpreter running the suite
        [sys.executable, "-m", module], cwd=ROOT, env=environ, capture_output=True, text=True, check=False, timeout=120
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def database(demo_role):  # noqa: ARG001 - skips for a superuser
    drop_database(URL)
    result = demo("--setup-only")
    assert result.returncode == 0, result.stderr
    assert "Created the demo database." in result.stdout
    yield URL
    drop_database(URL)


@pytest.fixture(scope="module")
def client(database):
    port = free_port()
    environ = {**os.environ, "ADORM_DEMO_DATABASE_URL": database}
    environ.pop("DJANGO_SETTINGS_MODULE", None)
    server = subprocess.Popen(  # noqa: S603 - the interpreter running the suite
        [sys.executable, "demo.py", "--no-setup", "--frontend", "none", "--port", str(port)],
        cwd=ROOT,
        env=environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60) as client:
            deadline = time.monotonic() + 120
            while True:
                if server.poll() is not None:
                    pytest.fail(f"demo.py exited: {server.stdout.read()}")
                try:
                    if client.get("/api/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline, "the demo API did not start within 120 s"
                time.sleep(0.1)  # readiness poll of a child process
            yield client
    finally:
        server.terminate()
        server.wait(timeout=30)


@pytest.fixture(scope="module")
def drivers(client):
    names = client.get("/api/health").json()["drivers"]
    missing = [name for name in REQUIRED if name not in names]
    assert not missing, f"required demo drivers are not available: {missing}"
    return names


@pytest.mark.usefixtures("database")
def test_setup_runs_again_without_changes():
    result = demo("--setup-only")
    assert result.returncode == 0, result.stderr
    assert "Created the demo database." not in result.stdout
    assert "Library data already present." in result.stdout
    assert "6 models checked, no problems found." in result.stdout


def test_setup_refuses_a_url_without_a_database():
    environ_url = with_database(TEST_DATABASE_URL, "postgres")
    result = subprocess.run(  # noqa: S603 - the interpreter running the suite
        [sys.executable, "demo.py", "--setup-only"],
        cwd=ROOT,
        env={**os.environ, "ADORM_DEMO_DATABASE_URL": environ_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "names no demo database" in result.stderr


def test_drivers_endpoint_lists_both_real_drivers(client, drivers):
    listed = {driver["name"]: driver for driver in client.get("/api/drivers").json()}
    assert set(listed) == {"django", "asyncpg"}
    assert [name for name, driver in listed.items() if driver["available"]] == drivers
    assert {listed[name]["mode"] for name in drivers} <= {"threaded", "async"}


def test_every_example_runs_on_every_driver_with_the_same_result(client, drivers):
    examples = client.get("/api/examples").json()
    assert len(examples) >= 10
    for example in examples:
        assert example["code"].startswith("async def ")
        answer = client.post(f"/api/examples/{example['id']}/run", json={})
        assert answer.status_code == 200, answer.text
        runs = answer.json()["runs"]
        assert [run["driver"] for run in runs] == drivers
        for run in runs:
            assert run["error"] is None, (example["id"], run)
            assert run["statements"], (example["id"], run["driver"])
            assert {statement["session"] for statement in run["statements"]} == {"example"}
            assert run["ms"] > 0
        if not example["writes"]:
            assert len({repr(run["result"]) for run in runs}) == 1, example["id"]


def test_write_examples_are_reverted(client, drivers):
    before = client.post("/api/examples/stock/run").json()["runs"][0]["result"]
    for _ in range(2):
        runs = client.post("/api/examples/lend/run", json={"params": {"reader": "eva@example.com"}}).json()["runs"]
        assert [run["result"]["available_before"] for run in runs] == [3] * len(drivers)
        for run in runs:
            sql = [statement["sql"] for statement in run["statements"]]
            assert sql[0] == "BEGIN"
            assert sql[-1] == "COMMIT"
            assert any("FOR UPDATE" in statement for statement in sql)
    assert client.post("/api/examples/stock/run").json()["runs"][0]["result"] == before


def test_statement_parameters_are_json(client):
    for run in client.post("/api/examples/enrol_readers/run", json={"params": {"count": 2}}).json()["runs"]:
        insert = next(s for s in run["statements"] if s["sql"].startswith("INSERT"))
        assert any(isinstance(param, list) for param in insert["params"]), insert


def test_custom_options_from_the_hook_and_the_block(client, drivers):
    for run in client.post("/api/examples/desk_options/run", json={}).json()["runs"]:
        assert run["error"] is None, run
        sql = [s["sql"] for s in run["statements"]]
        assert any(s.startswith('SET "app"."reader" = ') and "'example'" in s for s in sql), sql
        assert any(s.startswith('SET LOCAL "app"."reader" = ') and "'audit'" in s for s in sql), sql
        assert 'RESET "app"."reader"' in sql, sql
    assert len(drivers) >= 1


def test_example_parameters(client):
    runs = client.post("/api/examples/search_books/run", json={"params": {"search": "karamazov"}}).json()["runs"]
    for run in runs:
        assert [book["title"] for book in run["result"]] == ["The Brothers Karamazov"]
        assert "%karamazov%" in run["statements"][0]["params"]
    failed = client.post("/api/examples/edition_details/run", json={"params": {"isbn": "0000000000000"}})
    assert {run["error"]["error"] for run in failed.json()["runs"]} == {"DoesNotExist"}


@pytest.mark.parametrize(
    ("path", "body", "status"),
    [
        ("/api/examples/missing/run", {}, 404),
        ("/api/scenarios/missing/run", None, 404),
        ("/api/examples/search_books/run", {"params": {"colour": "red"}}, 422),
        ("/api/examples/search_books/run", {"params": {"genre": "comic"}}, 422),
        ("/api/examples/enrol_readers/run", {"params": {"count": "many"}}, 422),
        ("/api/examples/enrol_readers/run", {"params": {"count": 0}}, 422),
        ("/api/examples/enrol_readers/run", {"params": {"count": 21}}, 422),
        ("/api/examples/enrol_readers/run", {"params": {"count": None}}, 422),
        ("/api/examples/enrol_readers/run", {"params": {"count": [1]}}, 422),
        ("/api/examples/enrol_readers/run", {"params": {"count": 2.7}}, 422),
        ("/api/examples/enrol_readers/run", {"params": {"count": True}}, 422),
        ("/api/examples/search_books/run", {"params": {"search": None}}, 422),
    ],
)
def test_errors(client, path, body, status):
    assert client.post(path, json=body).status_code == status


def test_scenarios_endpoint(client):
    scenarios = client.get("/api/scenarios").json()
    assert [scenario["id"] for scenario in scenarios] == [
        "race",
        "lock_timeout",
        "cancel",
        "deadlock",
        "slot_deadlock",
        "pool_exhaustion",
    ]
    for scenario in scenarios:
        assert set(scenario) == {"id", "title", "description", "code", "choreography"}
        assert scenario["code"].startswith("async def ")
        assert scenario["choreography"].startswith(f"async def {scenario['id']}(run: Run)")


def scenario(client, drivers, name):
    """The runs of scenario ``name``, after checking what every run has."""
    answer = client.post(f"/api/scenarios/{name}/run")
    assert answer.status_code == 200, answer.text
    runs = answer.json()["runs"]
    assert [run["driver"] for run in runs] == drivers
    for run in runs:
        times = [event["at_ms"] for event in run["events"]]
        assert times == sorted(times)
        sessions = set(run["flows"]) | {f"{flow}2" for flow in run["flows"]}
        assert {statement["session"] for statement in run["statements"]} <= sessions
        assert_transactions_end(run["statements"])
        assert all(not (s["error"] or "").endswith(":") for s in run["statements"])
    # A connection the scenario set options on runs the next example without them.
    for example in client.post("/api/examples/dashboard/run", json={}).json()["runs"]:
        assert not [s["sql"] for s in example["statements"] if s["sql"].startswith(("SET", "RESET"))]
    return runs


def assert_transactions_end(statements):
    """Each session's ``BEGIN`` is followed by its ``COMMIT`` or ``ROLLBACK``."""
    open_by_session = {}
    for statement in statements:
        session, sql = statement["session"], statement["sql"]
        if sql == "BEGIN":
            assert not open_by_session.get(session), (session, statements)
            open_by_session[session] = True
        elif sql in ("COMMIT", "ROLLBACK"):
            open_by_session[session] = False
    assert not any(open_by_session.values()), open_by_session


def events(run, flow):
    return [event["text"] for event in run["events"] if event["flow"] == flow]


@pytest.mark.parametrize("repeat", range(REPEATS))
def test_race_for_one_row(client, drivers, repeat):  # noqa: ARG001 - repeated
    for run in scenario(client, drivers, "race"):
        assert run["outcomes"]["A"] == {"status": "ok", "result": "lent"}
        assert run["outcomes"]["B"]["error"] == "LookupError"
        assert events(run, "A")[1] == "has the row lock; 1 available"
        assert events(run, "B")[1] == "has the row lock; 0 available"
        assert events(run, "demo")[0].startswith("B waits for a lock held by database session")
        sessions = [statement["session"] for statement in run["statements"] if "FOR UPDATE" in statement["sql"]]
        assert sessions == ["A", "B"]
        commit = next(s for s in run["statements"] if s["session"] == "A" and s["sql"] == "COMMIT")
        assert commit["at_ms"] <= next(e["at_ms"] for e in run["events"] if e["text"].endswith("0 available"))


@pytest.mark.parametrize("repeat", range(REPEATS))
def test_lock_wait_with_timeout(client, drivers, repeat):  # noqa: ARG001 - repeated
    for run in scenario(client, drivers, "lock_timeout"):
        assert run["outcomes"]["A"]["status"] == "ok"
        assert run["outcomes"]["B"]["sqlstate"] == "55P03"
        assert run["outcomes"]["B"]["error"] == "OperationalError"
        assert any(s["session"] == "B" and s["sql"] == "SET LOCAL lock_timeout = 500" for s in run["statements"])
        assert run["ms"] >= 500


@pytest.mark.parametrize("repeat", range(REPEATS))
def test_cancellation_of_a_running_statement(client, drivers, repeat):  # noqa: ARG001 - repeated
    for run in scenario(client, drivers, "cancel"):
        assert run["outcomes"]["A"]["status"] == "ok"
        assert run["outcomes"]["B"] == {"status": "cancelled", "statement": "unknown"}
        assert events(run, "demo")[-1].startswith("1 slot in use")
        waiting = next(s for s in run["statements"] if s["session"] == "B" and "FOR UPDATE" in s["sql"])
        assert waiting["error"] is not None


@pytest.mark.parametrize("repeat", range(REPEATS))
def test_detected_deadlock(client, drivers, repeat):  # noqa: ARG001 - repeated
    for run in scenario(client, drivers, "deadlock"):
        outcomes = run["outcomes"]
        failed = [flow for flow, outcome in outcomes.items() if outcome["status"] == "error"]
        assert len(failed) == 1, outcomes
        assert outcomes[failed[0]]["sqlstate"] == "40P01"
        (other,) = set(outcomes) - set(failed)
        assert outcomes[other] == {"status": "ok", "result": "locked both"}


@pytest.mark.parametrize("repeat", range(REPEATS))
def test_deadlock_on_the_slots(client, drivers, repeat):  # noqa: ARG001 - repeated
    for run in scenario(client, drivers, "slot_deadlock"):
        outcomes = run["outcomes"]
        assert outcomes["A"] == {"status": "ok", "result": 24}
        assert outcomes["B"]["error"] == "DeadlockError"
        assert outcomes["B"]["held_sessions"] == ["B"]
        asked = next(e["at_ms"] for e in run["events"] if e["flow"] == "B" and "asks for a second slot" in e["text"])
        failed = next(e["at_ms"] for e in run["events"] if e["flow"] == "B" and e["kind"] == "error")
        assert failed - asked < 500, "DeadlockError comes at once, not after acquire_timeout"
        assert [e["text"] for e in run["events"] if e["flow"] == "demo"][1] == "A waits for a slot B holds"
        if run["driver"] == "django":
            # B's transaction runs no statement, so psycopg sends no BEGIN for it.
            assert not [s for s in run["statements"] if s["session"] == "B" and s["sql"] == "BEGIN"]
        assert any(statement["session"] == "A2" for statement in run["statements"])
        assert not [statement for statement in run["statements"] if statement["session"] == "B2"]


@pytest.mark.parametrize("repeat", range(REPEATS))
def test_pool_exhaustion(client, drivers, repeat):  # noqa: ARG001 - repeated
    for run in scenario(client, drivers, "pool_exhaustion"):
        outcomes = run["outcomes"]
        assert outcomes["C"]["error"] == "SessionTimeout"
        assert [outcomes[flow]["status"] for flow in "ABD"] == ["ok", "ok", "ok"]
        assert outcomes["D"]["result"] == 24
        assert events(run, "demo")[:2] == [
            "2 of 2 slots in use",
            "C waits for a slot (1 waiting, acquire_timeout 1 s)",
        ]
        assert not [statement for statement in run["statements"] if statement["session"] == "C"]


def test_scenarios_restore_the_data(client):
    before = client.post("/api/examples/stock/run").json()["runs"][0]["result"]
    for name in ("race", "deadlock", "slot_deadlock"):
        client.post(f"/api/scenarios/{name}/run")
    assert client.post("/api/examples/stock/run").json()["runs"][0]["result"] == before


def test_a_failing_choreography_releases_its_flows(database):
    result = demo_module("tests.demo.failing_choreography", database)
    assert result.returncode == 0, result.stderr
    runs = [json.loads(line) for line in result.stdout.splitlines()]
    assert runs
    for run in runs:
        assert run["error"] == "RuntimeError"
        assert run["failed_ms"] < 5000
        assert run["flows_left"] == []
        assert run["race"] == {"A": "ok", "B": "error"}


def test_a_cancelled_run_restores_the_data(database):
    result = demo_module("tests.demo.cancelled_runs", database)
    assert result.returncode == 0, result.stderr
    runs = [json.loads(line) for line in result.stdout.splitlines()]
    assert {run["kind"] for run in runs} == {"baseline", "example", "undo", "scenario", "setup"}
    for run in runs:
        assert run["outcome"] in ("cancelled", "finished"), run
        assert not run["capture_active"], run
        if run["kind"] in ("baseline", "scenario", "setup"):
            assert run["restored"], run
            assert run.get("flows_left", []) == [], run
        else:
            assert not run["written"], run
    cancelled = [run for run in runs if run["hops"] == 0]
    assert cancelled
    assert all(run["outcome"] == "cancelled" for run in cancelled), cancelled
