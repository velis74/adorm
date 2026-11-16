"""The demo on a SQLite database: ``demo.py`` sets it up and serves the API on the Django and memory drivers."""

import os
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest

from demo.api import drivers
from demo.database import drop_database
from tests.demo.test_smoke import Environment, free_port, ROOT, run_demo, STARTUP_SECONDS


@pytest.fixture(scope="module")
def sqlite_env(tmp_path_factory):
    url = f"sqlite:///{tmp_path_factory.mktemp('demo') / 'demo.sqlite3'}"
    env = Environment(os.environ, ADORM_DEMO_DATABASE_URL=url)
    env.pop(drivers.DRIVER_ENV, None)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    try:
        yield env
    finally:
        drop_database(url)


@pytest.fixture(scope="module")
def sqlite_database(sqlite_env):
    result = run_demo(sqlite_env, "--setup-only")
    assert result.returncode == 0, result.stderr
    assert "Demo data created." in result.stdout
    assert "11 adorm models match the Django models." in result.stdout
    path = sqlite_env["ADORM_DEMO_DATABASE_URL"].removeprefix("sqlite:///")
    db = sqlite3.connect(path)
    try:
        yield db
    finally:
        db.close()


def test_the_asyncpg_driver_is_not_available_on_sqlite(sqlite_env):
    result = run_demo(sqlite_env, "--list-drivers")
    assert result.returncode == 0, result.stderr
    assert "not available: the demo database is SQLite" in next(
        line for line in result.stdout.splitlines() if line.startswith("asyncpg")
    )


@pytest.fixture(params=["django", "memory"])
def sqlite_server(request, sqlite_env, sqlite_database):  # noqa: ARG001 - needs the database set up
    port = free_port()
    args = [sys.executable, "demo.py", "--no-setup", "--port", str(port), "--driver", request.param]
    process = subprocess.Popen(  # noqa: S603 - fixed command
        args, cwd=ROOT, env=sqlite_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + STARTUP_SECONDS
        while True:
            if process.poll() is not None:
                pytest.fail(f"demo.py exited with {process.returncode}:\n{process.stdout.read()}")
            try:
                if httpx.get(f"{base}/api/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("demo.py did not start")
            time.sleep(0.1)
        yield request.param, base
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()


def test_the_api_reads_and_writes_on_sqlite(sqlite_server, sqlite_database):
    driver, base = sqlite_server
    members_before = sqlite_database.execute("SELECT count(*) FROM clubs_member").fetchone()[0]
    with httpx.Client(base_url=base, timeout=10) as http:
        assert http.get("/api/health").json()["driver"] == driver
        assert [d["name"] for d in http.get("/api/drivers").json() if d["available"]] == ["django", "memory"]
        members = http.get("/api/data/members", params={"limit": 100}).json()
        assert len(members["result"]) == members_before
        comparison = http.get("/api/data/compare/members", params={"limit": 100}).json()
        assert [(read["driver"], read["error"]) for read in comparison["reads"]] == [
            ("django", None),
            ("memory", None),
        ]
        assert comparison["same"] is True
        summary = http.get("/api/data/clubs/ljubljana-rowing/summary", params={"atomic": True})
        assert summary.status_code == 200, summary.text
        enrolment = {"first_name": "Mia", "last_name": "Kos", "email": f"mia-{driver}@example.com", "fee": "50.00"}
        enrolled = http.post("/api/data/clubs/koper-sailing/members", json=enrolment)
        assert enrolled.status_code == 201, enrolled.text
        member_id = enrolled.json()["result"]["member"]["id"]
        assert http.get(f"/api/data/members/{member_id}").json()["result"]["first_name"] == "Mia"
