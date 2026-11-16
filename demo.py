"""
Runs the demo: prepares the PostgreSQL database and serves the API.

    uv run python demo.py                      # first available driver
    uv run python demo.py --driver asyncpg     # a given driver
    uv run python demo.py --reset              # refill the demo data first
    uv run python demo.py --list-drivers

The database is ``ADORM_DEMO_DATABASE_URL`` (default ``postgres://postgres@127.0.0.1:5432/adorm_demo``), from the
environment or the file ``.env`` in the repository root. Setup
creates the database when the server lacks it, applies the Django migrations, seeds the demo data when the database
has none, checks that the generated models in ``demo/models`` are up to date (``adorm generate --check``) and compares
them with the Django models (``manage.py check_adorm_models``).
"""

import argparse
import os
import subprocess
import sys

from pathlib import Path

from dotenv import load_dotenv

from demo.api import drivers
from demo.database import DEFAULT_DATABASE_NAME, DEFAULT_SERVER_URL, ensure_database, with_database

DATABASE_ENV = "ADORM_DEMO_DATABASE_URL"
ROOT = Path(__file__).parent
GENERATE = (
    "generate",
    "--settings",
    "demo.backend.settings",
    "--pythonpath",
    str(ROOT),
    "--output",
    str(ROOT / "demo" / "models"),
    "--line-length",
    "120",
)
"""Arguments of ``adorm`` that regenerate the demo's models."""


def manage(*args: str, env: dict[str, str]) -> None:
    """Run a command of the demo's Django project in a child process; exit when it fails."""
    command = [sys.executable, "-m", "demo.backend.manage", *args]
    if subprocess.run(command, env=env, check=False).returncode:  # noqa: S603 - fixed command
        raise SystemExit(f"manage.py {' '.join(args)} failed")


def check_generated(env: dict[str, str]) -> None:
    """Run ``adorm generate --check`` for the demo's models in a child process; exit when they are not up to date."""
    command = [sys.executable, "-m", "adorm", *GENERATE, "--check"]
    if subprocess.run(command, env=env, check=False).returncode:  # noqa: S603 - fixed command
        raise SystemExit(
            "demo/models is not up to date; regenerate it with\n"
            "  uv run adorm generate --settings demo.backend.settings -o demo/models --line-length 120"
        )


def setup(env: dict[str, str], *, reset: bool) -> None:
    if ensure_database(env[DATABASE_ENV]):
        print("Created the demo database.", flush=True)
    manage("migrate", "--verbosity", "0", env=env)
    manage("seed_demo", *(["--reset"] if reset else []), env=env)
    check_generated(env)
    manage("check_adorm_models", env=env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--driver", choices=[d.name for d in drivers.DRIVERS], help="driver to run the API on")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reset", action="store_true", help="replace the demo data with the fixed data set")
    parser.add_argument("--no-setup", action="store_true", help="skip database setup")
    parser.add_argument("--setup-only", action="store_true", help="set up the database and exit")
    parser.add_argument("--list-drivers", action="store_true", help="list the drivers and exit")
    args = parser.parse_args(argv)

    if args.list_drivers:
        for driver in drivers.DRIVERS:
            reason = driver.unavailable_reason()
            state = "available" if reason is None else f"not available: {reason}"
            print(f"{driver.name:10} {driver.target:40} {state}")
        return 0

    load_dotenv(ROOT / ".env")
    env = dict(os.environ)
    env.setdefault(DATABASE_ENV, with_database(DEFAULT_SERVER_URL, DEFAULT_DATABASE_NAME))
    if args.driver:
        env[drivers.DRIVER_ENV] = args.driver
    try:
        driver = drivers.selected(env)
    except (LookupError, RuntimeError) as exc:
        parser.error(str(exc))

    if not args.no_setup:
        setup(env, reset=args.reset)
    if args.setup_only:
        return 0

    os.environ.update(env)
    print(f"Driver:        {driver or 'none available'}")
    print(f"API docs:      http://{args.host}:{args.port}/docs")
    import uvicorn

    uvicorn.run("demo.api.main:app", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
