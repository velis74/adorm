# Demo application

The repository contains a demo application: a Django project with a small club domain, a FastAPI app whose endpoints
use adorm, PostgreSQL, and the launcher `demo.py`. Its endpoints build adorm operations and answer them encoded,
together with what each available driver does not support; nothing is executed. The active driver (`--driver`, or the
first available one) is reported by `/api/health` and `/api/drivers`.

## Running it

The demo needs a PostgreSQL server. `ADORM_DEMO_DATABASE_URL` names the database (default
`postgres://postgres@127.0.0.1:5432/adorm_demo`).

```bash
uv sync
export ADORM_DEMO_DATABASE_URL=postgres://user:password@127.0.0.1:5432/adorm_demo
uv run python demo.py
```

| Option | Effect |
|---|---|
| `--driver NAME` | run the API on that driver (`django`, `asyncpg`); default: the first available |
| `--list-drivers` | list the drivers, their classes and whether each is available (with the reason when not), then exit |
| `--reset` | replace the demo data with the fixed data set |
| `--setup-only` | set up the database, then exit |
| `--no-setup` | skip the database setup |
| `--host`, `--port` | where the API listens (default `127.0.0.1:8000`) |

```text
$ uv run python demo.py --list-drivers
django     adorm.drivers.django:DjangoDriver        not available: adorm.drivers.django does not exist
asyncpg    adorm.drivers.asyncpg:AsyncpgDriver      not available: adorm.drivers.asyncpg does not exist
```

Before serving, `demo.py` sets up the database:

1. creates the database when the server lacks it;
2. applies the Django migrations (`manage.py migrate`);
3. fills the database with the fixed data set when it has no clubs (`manage.py seed_demo`; `--reset` replaces the
   data);
4. compares the adorm models with the Django models (`manage.py check_adorm_models`, exit status 1 on a difference):
   the primary key name; each concrete field's presence, Python type, nullability, choices (against a `Literal`),
   `max_length`, `max_digits` and `decimal_places`; fields Django lacks; `Meta.ordering`, which adorm models cannot
   express; each relation's kind, target and join columns, and the through model of a many-to-many relation; Django
   foreign keys and many-to-many fields the adorm model lacks. Defaults, validators and the generic foreign key are
   not compared.

The API documentation is served at `http://127.0.0.1:8000/docs` (Swagger UI) and `/redoc`.

## Layout

| Path | Contents |
|---|---|
| `demo.py` | launcher: database setup, then uvicorn serving `demo.api.main:app` |
| `demo/database.py` | database URL helpers, creating and dropping the database |
| `demo/backend/` | Django project: `settings.py`, `manage.py` and the apps `clubs`, `activities` and `billing` |
| `demo/backend/model_check.py` | comparison of an adorm model with the Django model of the same label |
| `demo/api/models.py` | adorm models, one per Django model and `contenttypes.ContentType`, labelled like the Django models (`clubs.Member`) |
| `demo/api/operations.py` | functions building the operation of each endpoint from its parameters |
| `demo/api/drivers.py` | the drivers the demo runs against and the active driver |
| `demo/api/main.py` | the FastAPI app |
| `tests/demo/` | in-process tests of the API and the end-to-end test of `demo.py` |

Django runs the migrations, the seed command and the model check. The API process does not query through Django: it
builds adorm operations.

## Domain

| App | Model | Constructs it exercises |
|---|---|---|
| `clubs` | `Club` | unique and slug fields, a JSON field, many-to-many `members` through `Membership` |
| `clubs` | `Member` | choices, nullable date, JSON list |
| `clubs` | `Membership` | two foreign keys, a unique constraint, a decimal with a validator |
| `activities` | `Tag` | the target of a plain many-to-many (`Activity.tags`) |
| `activities` | `Activity` | a nullable foreign key (`coach`, `SET_NULL`), a duration, many-to-many through `Attendance` |
| `activities` | `Competition` | multi-table inheritance from `Activity` (primary key `activity_ptr_id`); one row is added to an existing activity |
| `activities` | `Attendance` | a nullable datetime, a unique constraint |
| `billing` | `Invoice` | `PROTECT` foreign key, `clean()` comparing two dates (`full_clean`) |
| `billing` | `InvoiceLine` | a generic foreign key `item` to a `Membership` or an `Activity`, through `contenttypes.ContentType` |
| `billing` | `Payment` | a UUID primary key (`code`) |

The Django models have no default ordering (`Meta.ordering`): adorm models cannot express it, so it would apply on
one driver only.

An adorm model declares the Django model's concrete fields (foreign keys by their column, `club_id`) as pydantic
fields, its foreign keys as `ForeignKey` descriptors, reverse foreign keys as `ReverseForeignKey` and many-to-many
relations as `ManyToMany` with their join columns. Query paths traverse all of them (`memberships__club__slug`). An
MTI child is a subclass of the parent's adorm model with its own label and primary key. The generic foreign key
`InvoiceLine.item` has no adorm counterpart; its columns `content_type_id` and `object_id` are fields.

```python
from adorm import ForeignKey, ManyToMany, Model, ModelMeta, ReverseForeignKey


class Membership(Model):
    _meta = ModelMeta("clubs.Membership")
    id: int | None = None
    club_id: int
    member_id: int
    role: Role = "member"
    since: datetime.date
    fee: decimal.Decimal = Field(ge=0, max_digits=8, decimal_places=2)
    active: bool = True
    club = ForeignKey("clubs.Club")
    member = ForeignKey("clubs.Member")


class Club(Model):
    _meta = ModelMeta("clubs.Club")
    ...
    memberships = ReverseForeignKey("clubs.Membership", column="club_id")
    members = ManyToMany("clubs.Member", through="clubs.Membership", source_column="club_id", target_column="member_id")


class Competition(Activity):
    _meta = ModelMeta("activities.Competition", pk="activity_ptr_id")
    activity_ptr_id: int | None = None
    discipline: str = Field(max_length=60)
    entry_deadline: datetime.date | None = None
    activity_ptr = ForeignKey("activities.Activity")
```

## Endpoints

Every endpoint under `/api/operations/` answers an `OperationPlan` (`POST /api/operations/invoices/{pk}/payments`
answers a list of two):

| Field | Contents |
|---|---|
| `operation` | the operation as [`Operation.to_dict()`](/ref/encoding) encodes it with the models' types |
| `constructs` | the construct names the operation uses, sorted, without repeats |
| `unsupported` | per available driver: the operation kind and the constructs that driver does not support |

| Method and path | Operation | Constructs shown |
|---|---|---|
| `GET /api/operations/members` | `Select` | `Q` with `\|`, lookups across relations, `distinct()`, ordering, slicing |
| `GET /api/operations/clubs/{slug}/report` | `Get` | `Count` with `filter`, `Sum` and `Count` in grouped subqueries with `Coalesce`, `Exists`, `Subquery`, `Case` / `When` |
| `GET /api/operations/activities` | `Select` | `select_related()`, filtered `Count`, `F` arithmetic, `TruncMonth` |
| `GET /api/operations/invoices/outstanding` | `Select` | `values()` followed by `annotate()` (grouping) |
| `POST /api/operations/members` | `Create` | request body validated by the adorm model `Member` |
| `PATCH /api/operations/members/{pk}` | `Save` | `update_fields` from the fields in the body; `null` only for `birth_date` |
| `POST /api/operations/activities/{pk}/attendances` | `GetOrCreate` | lookups and defaults |
| `POST /api/operations/attendances/{pk}/cancel` | `Update` | `exclude()` |
| `POST /api/operations/invoices/{pk}/payments` | `Create`, `Update` | two operations, answered as a list |
| `DELETE /api/operations/clubs/{slug}/draft-invoices` | `Delete` | a filter across a relation |
| `POST /api/operations/decode` | any | decoding an encoded operation; invalid data answers 422 |

Every 422 answer has FastAPI's validation error body (`detail`: a list of errors with `type`, `loc` and `msg`), also
for the checks an endpoint makes after request validation: unknown fields of a new member, a change without fields or
with `null` for a field other than `birth_date`, and an operation `Operation.from_dict()` rejects.

| Method and path | Answer |
|---|---|
| `GET /api/health` | `status`, `adorm_version` and the active `driver` |
| `GET /api/drivers` | every driver the demo knows: availability, whether it is active, its mode and operation kinds |
| `GET /api/models` | the JSON schema of every adorm model by label |

```bash
curl 'http://127.0.0.1:8000/api/operations/members?club=ljubljana-rowing&search=nov&limit=5'
```

Abbreviated (`…` marks omitted keys):

```json
{
  "operation": {
    "operation": "select",
    "query": {
      "node": "Query",
      "model": "clubs.Member",
      "where": {
        "node": "Q",
        "children": [
          {"node": "lookup.exact", "lhs": {"node": "F", "name": "memberships__active"}, "rhs": {"value": true}},
          {"node": "lookup.exact", "lhs": {"node": "F", "name": "memberships__club__slug"},
           "rhs": {"value": "ljubljana-rowing"}},
          {"node": "Q", "children": ["…two lookup.icontains nodes…"], "connector": "OR", "negated": false}
        ],
        "connector": "AND",
        "negated": false
      },
      "…": "…"
    }
  },
  "constructs": ["clause.distinct", "lookup.exact", "lookup.icontains"],
  "unsupported": {}
}
```

The operation behind an endpoint is a function of its parameters:

```python
from adorm.models import registry

from demo.api import operations

operation = operations.club_report("koper-sailing")     # a Get of clubs.Club with annotations
operation.to_dict(registry)                              # what the endpoint answers under "operation"
```

## Drivers

`demo/api/drivers.py` lists the drivers by import path. A driver is available when its module imports. It is not
available when the module does not exist or needs a package that is not installed; any other import error stops the
demo. The active driver comes from `--driver` (environment variable `ADORM_DEMO_DRIVER`), else it is the first
available one. At startup the API runs the active driver's setup (for the Django driver, `django.setup()` with the
demo settings) and instantiates it; the other available drivers are imported without setup. For every endpoint the
API reports what each available driver does not support, read from the driver class's `capabilities`.

```python
DRIVERS = (
    DemoDriver("django", "adorm.drivers.django:DjangoDriver", setup_django),
    DemoDriver("asyncpg", "adorm.drivers.asyncpg:AsyncpgDriver"),
)
```

## Tests

`tests/demo/test_api.py` calls every endpoint in process and checks the operation it answers against the builder in
`demo/api/operations.py`, the round trip through `/api/operations/decode`, the rejection of invalid input and the
driver matrix with a fake driver. `tests/demo/test_smoke.py` runs `demo.py` against the database `adorm_test_demo`:
the setup, its repetition and `--reset`, the club report's aggregates on the seeded data, the model check and the
`check_adorm_models` exit status, then the server once per available driver (once without a driver when none is
available), driven over HTTP. `ADORM_DEMO_REQUIRED_DRIVERS` (comma-separated driver names) makes the suite fail when
one of those drivers is not available. Both run in `uv run pytest`, and so in CI.
