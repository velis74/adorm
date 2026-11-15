"""The demo end to end: ``demo.py`` sets up a PostgreSQL database and serves the API, once per available driver."""

import os
import socket
import subprocess
import sys
import time

from decimal import Decimal
from pathlib import Path

import httpx
import psycopg
import pytest

from demo.api import drivers
from demo.database import drop_database, with_database

ROOT = Path(__file__).parent.parent.parent
DATABASE = "adorm_test_demo"
STARTUP_SECONDS = 30
REQUIRED_DRIVERS_ENV = "ADORM_DEMO_REQUIRED_DRIVERS"


def driver_matrix() -> list[str | None]:
    """Every available driver; ``None`` (no active driver) when there is none."""
    return drivers.available() or [None]


def test_required_drivers_are_available():
    """``ADORM_DEMO_REQUIRED_DRIVERS`` (comma-separated names) lists drivers whose absence fails the suite."""
    required = [name for name in os.environ.get(REQUIRED_DRIVERS_ENV, "").split(",") if name.strip()]
    reasons = {name: drivers.get(name.strip()).unavailable_reason() for name in required}
    assert {name: reason for name, reason in reasons.items() if reason is not None} == {}


class Environment(dict):
    """Environment variables of the demo processes; its repr hides the values (the database password)."""

    def __repr__(self) -> str:
        return f"<demo environment, {len(self)} variables>"


@pytest.fixture(scope="module")
def demo_env(database_url):
    url = with_database(database_url, DATABASE)
    drop_database(url)
    env = Environment(os.environ, ADORM_DEMO_DATABASE_URL=url)
    env.pop(drivers.DRIVER_ENV, None)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    try:
        yield env
    finally:
        drop_database(url)


def run_demo(env, *args, timeout=120):
    return subprocess.run(  # noqa: S603 - fixed command
        [sys.executable, "demo.py", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def database(demo_env):
    """The demo database after ``demo.py --setup-only``."""
    result = run_demo(demo_env, "--setup-only")
    assert result.returncode == 0, result.stderr
    assert "Created the demo database." in result.stdout
    assert "Demo data created." in result.stdout
    assert "11 adorm models match the Django models." in result.stdout
    with psycopg.connect(demo_env["ADORM_DEMO_DATABASE_URL"]) as conn:
        yield conn


def count(conn, table):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_setup_seeds_the_domain(database):
    assert count(database, "clubs_club") == 3
    assert count(database, "clubs_member") == 12
    assert count(database, "clubs_membership") == 13
    assert count(database, "activities_activity") == 7
    competitions = database.execute(
        "SELECT a.name, c.discipline FROM activities_competition c"
        " JOIN activities_activity a ON a.id = c.activity_ptr_id ORDER BY a.id"
    ).fetchall()
    assert competitions == [("Spring regatta", "Coxed eights"), ("Regional bouldering cup", "Bouldering")]
    assert count(database, "activities_activity_tags") > 0
    assert count(database, "billing_invoice") == count(database, "billing_invoiceline")
    generic = database.execute(
        "SELECT DISTINCT ct.model FROM billing_invoiceline l JOIN django_content_type ct ON ct.id = l.content_type_id"
    ).fetchall()
    assert sorted(row[0] for row in generic) == ["activity", "membership"]


def test_setup_again_keeps_the_data(demo_env, database):
    before = count(database, "billing_invoice")
    result = run_demo(demo_env, "--setup-only")
    assert result.returncode == 0, result.stderr
    assert "Demo data already present." in result.stdout
    assert count(database, "billing_invoice") == before


def test_reset_replaces_the_data_with_the_same_set(demo_env, database):
    tables = ("clubs_member", "activities_attendance", "activities_competition", "billing_invoice", "billing_payment")
    before = {table: count(database, table) for table in tables}
    result = run_demo(demo_env, "--setup-only", "--reset")
    assert result.returncode == 0, result.stderr
    assert "Demo data created." in result.stdout
    assert {table: count(database, table) for table in tables} == before
    assert database.execute("SELECT min(number) FROM billing_invoice").fetchone()[0] == "2026-0001"


CLUB_REPORT = """
import os
os.environ["DJANGO_SETTINGS_MODULE"] = "demo.backend.settings"
import django
django.setup()
from decimal import Decimal
from django.db.models import Count, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from demo.backend.billing.models import Invoice
from demo.backend.clubs.models import Club, Membership

fees = Membership.objects.filter(club=OuterRef("pk"), active=True).values("club").annotate(total=Sum("fee"))
invoices = Invoice.objects.filter(club=OuterRef("pk"), status="open").values("club").annotate(number=Count("pk"))
rows = Club.objects.annotate(
    active_members=Count("memberships", filter=Q(memberships__active=True)),
    fees=Coalesce(Subquery(fees.values("total")), Value(Decimal(0))),
    open_invoices=Coalesce(Subquery(invoices.values("number")), Value(0)),
).order_by("slug")
for row in rows:
    print(row.slug, row.active_members, row.fees, row.open_invoices)
"""


def test_club_report_aggregates_match_the_data(demo_env, database):
    """The club report's aggregate shape, run through Django on the seeded data, gives the per-club totals."""
    result = subprocess.run(  # noqa: S603 - fixed command
        [sys.executable, "-c", CLUB_REPORT], cwd=ROOT, env=demo_env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    expected = database.execute(
        "SELECT c.slug,"
        " (SELECT count(*) FROM clubs_membership m WHERE m.club_id = c.id AND m.active),"
        " (SELECT coalesce(sum(fee), 0) FROM clubs_membership m WHERE m.club_id = c.id AND m.active),"
        " (SELECT count(*) FROM billing_invoice i WHERE i.club_id = c.id AND i.status = 'open')"
        " FROM clubs_club c ORDER BY c.slug"
    ).fetchall()
    assert result.stdout.splitlines() == [" ".join(str(value) for value in row) for row in expected]
    assert [row[2] for row in expected] == [Decimal("300.00"), Decimal("300.00"), Decimal("382.00")]


def test_list_drivers(demo_env):
    result = run_demo(demo_env, "--list-drivers")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert [line.split()[0] for line in lines] == [d.name for d in drivers.DRIVERS]
    available = set(drivers.available())
    for line in lines:
        name = line.split()[0]
        assert ("not available" not in line) == (name in available)


def test_an_unavailable_driver_is_refused(demo_env):
    missing = [d.name for d in drivers.DRIVERS if not d.is_available()]
    if not missing:
        pytest.skip("every driver is available")
    result = run_demo(demo_env, "--driver", missing[0], "--no-setup")
    assert result.returncode == 2
    assert "is not available" in result.stderr


@pytest.fixture(params=driver_matrix(), ids=lambda name: name or "no-driver")
def server(request, demo_env, database):  # noqa: ARG001 - needs the database set up
    port = free_port()
    args = [sys.executable, "demo.py", "--no-setup", "--port", str(port)]
    if request.param is not None:
        args += ["--driver", request.param]
    process = subprocess.Popen(  # noqa: S603 - fixed command
        args, cwd=ROOT, env=demo_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
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


def test_api_serves_operations(server):
    driver, base = server
    with httpx.Client(base_url=base, timeout=10) as http:
        assert http.get("/api/health").json()["driver"] == driver
        listed = {d["name"]: d for d in http.get("/api/drivers").json()}
        if driver is not None:
            assert listed[driver]["active"]
        reads = [
            ("/api/operations/members", {"club": "ljubljana-rowing", "search": "nov"}),
            ("/api/operations/clubs/ljubljana-rowing/report", {}),
            ("/api/operations/activities", {"kind": "training"}),
            ("/api/operations/invoices/outstanding", {"today": "2026-03-01"}),
        ]
        for path, params in reads:
            response = http.get(path, params=params)
            assert response.status_code == 200, (path, response.text)
            body = response.json()
            assert sorted(body["unsupported"]) == sorted(drivers.available())
            decoded = http.post("/api/operations/decode", json=body["operation"])
            assert decoded.json() == body
        member = {"first_name": "Mia", "last_name": "Kos", "email": "mia@example.com", "joined": "2026-01-01T09:00:00Z"}
        assert http.post("/api/operations/members", json=member).status_code == 201
        assert http.post("/api/operations/members", json={**member, "level": "expert"}).status_code == 422
        assert http.patch("/api/operations/members/1", json={"level": "advanced"}).status_code == 200
        assert http.patch("/api/operations/members/1", json={"level": None}).status_code == 422
        assert http.post("/api/operations/activities/1/attendances", json={"member_id": 2}).status_code == 200
        assert http.post("/api/operations/attendances/1/cancel").status_code == 200
        payment = {"amount": "120.00", "paid_at": "2026-02-01T12:00:00Z"}
        assert len(http.post("/api/operations/invoices/1/payments", json=payment).json()) == 2
        assert http.delete("/api/operations/clubs/koper-sailing/draft-invoices").status_code == 200
        assert len(http.get("/api/models").json()) == 11
        assert http.get("/openapi.json").status_code == 200


MISMATCH = """
import os, datetime, decimal
os.environ["DJANGO_SETTINGS_MODULE"] = "demo.backend.settings"
import django
django.setup()
from typing import Literal
from django.apps import apps
from pydantic import Field
from adorm import ForeignKey, ManyToMany, Model, ModelMeta, ReverseForeignKey
from adorm.models import Relation
from demo.backend.model_check import compare

apps.get_model("clubs.Club")._meta.ordering = ["name"]


class Club(Model):
    _meta = ModelMeta("clubs.Club", pk="name")
    id: int | None = None
    name: str | None = None
    founded: datetime.datetime
    city: str = Field(max_length=50)
    address: dict
    nickname: str
    owner = ForeignKey("clubs.Member", column="city")
    memberships = Relation("clubs.Member")
    sponsors = Relation("clubs.Member")
    activities = ReverseForeignKey("activities.Activity", column="coach_id")
    invoices = ManyToMany("billing.Invoice", through="billing.Invoice", source_column="club_id", target_column="id")


class Membership(Model):
    _meta = ModelMeta("clubs.Membership")
    id: int | None = None
    club_id: str
    member_id: int
    role: Literal["member", "coach"] = "member"
    since: datetime.date
    fee: decimal.Decimal = Field(max_digits=6, decimal_places=2)
    active: bool = True
    member = ForeignKey("clubs.Member")


class Activity(Model):
    _meta = ModelMeta("activities.Activity")
    id: int | None = None
    club_id: int
    coach_id: int | None = None
    name: str = Field(max_length=100)
    kind: Literal["training", "competition", "social"] = "training"
    starts_at: datetime.datetime
    duration: datetime.timedelta
    capacity: int
    price: decimal.Decimal = Field(max_digits=8, decimal_places=2)
    club = ForeignKey("clubs.Club")
    coach = ForeignKey("clubs.Member")
    tags = ManyToMany(
        "activities.Tag", through="activities.Activity_tags", source_column="tag_id", target_column="activity_id"
    )
    participants = ManyToMany(
        "clubs.Member", through="activities.Attendance", source_column="activity_id", target_column="member_id"
    )


for model in (Club, Membership, Activity):
    print("\\n".join(compare(model)))
"""


MEMBERSHIP_PROBLEMS = [
    "clubs.Membership.club_id: Django type int, the adorm field is <class 'str'>",
    "clubs.Membership.role: choices ['board', 'coach', 'member'], the adorm Literal has ['coach', 'member']",
    "clubs.Membership.fee: max_digits=8, the adorm field has 6",
    "clubs.Membership.club: the Django relation is missing",
]


def test_model_check_reports_every_difference(demo_env):
    result = subprocess.run(  # noqa: S603 - fixed command
        [sys.executable, "-c", MISMATCH], cwd=ROOT, env=demo_env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "clubs.Club: primary key 'name', Django has 'id'",
        "clubs.Club: Meta.ordering ['name'], which adorm models cannot express",
        "clubs.Club.name: null=False, the adorm field disagrees",
        "clubs.Club.name: max_length=100, the adorm field has None",
        "clubs.Club.slug is missing",
        "clubs.Club.founded: Django type date, the adorm field is <class 'datetime.datetime'>",
        "clubs.Club.city: max_length=100, the adorm field has 50",
        "clubs.Club.nickname is not a Django field",
        "clubs.Club.owner is not a Django relation",
        "clubs.Club.memberships targets clubs.Member, Django clubs.Membership",
        "clubs.Club.memberships is a bare Relation without join columns",
        "clubs.Club.sponsors is not a Django relation",
        "clubs.Club.activities is a ReverseForeignKey on 'coach_id', Django disagrees",
        "clubs.Club.invoices is a ManyToMany, Django disagrees",
        "clubs.Club.members: the Django relation is missing",
        *MEMBERSHIP_PROBLEMS,
        "activities.Activity.tags is a ManyToMany through ('activities.Activity_tags', 'tag_id', 'activity_id'), Django"
        " through ('activities.Activity_tags', 'activity_id', 'tag_id')",
    ]


def test_check_adorm_models_command_exits_1_on_a_difference(demo_env):
    script = MISMATCH.split("for model in")[0] + (
        "from adorm.models import registry\n"
        "for model in (Club, Membership, Activity):\n"
        "    registry.unregister(model._meta.label)\n"
        "import demo.backend.clubs.management.commands.check_adorm_models as command\n"
        "from django.core.management import call_command\n"
        "command.MODELS = (Membership,)\n"
        "call_command('check_adorm_models')\n"
    )
    result = subprocess.run(  # noqa: S603 - fixed command
        [sys.executable, "-c", script], cwd=ROOT, env=demo_env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 1
    assert result.stderr.splitlines() == MEMBERSHIP_PROBLEMS
    assert result.stdout == ""
