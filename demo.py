"""
Runs the demo: sets up the PostgreSQL database, then serves the API and the frontend.

    uv run python demo.py                      # API and the Vite dev server
    uv run python demo.py --frontend built     # API serving the built frontend
    uv run python demo.py --reset              # refill the demo data first
    uv run python demo.py --list-drivers

The database is ``ADORM_DEMO_DATABASE_URL``, from the environment or the file ``.env`` in the repository root. Its user
is a dedicated role with CREATEDB, not a superuser; setup creates the database owned by it when the server lacks it,
applies the Django migrations, seeds the library data when the database has none, checks that the generated models in
``demo/models`` are up to date (``adorm generate --check``) and that the tables match them (``adorm check-schema``).
"""

import argparse
import os
import signal
import subprocess
import sys

from contextlib import suppress
from pathlib import Path

from dotenv import load_dotenv

from demo.api import drivers
from demo.database import check_url, DATABASE_ENV, DatabaseSetupError, drop_database, ensure_database, with_database

ROOT = Path(__file__).parent
FRONTEND_DIST = ROOT / "demo" / "frontend" / "dist"
GENERATE = ("generate", "--settings", "demo.backend.settings", "--output", str(ROOT / "demo" / "models"))
GENERATE += ("--line-length", "120", "--pythonpath", str(ROOT))
STOP_GRACE_SECONDS = 5


def run(command: list[str], env: dict[str, str], what: str) -> None:
    """Run ``command`` in a child process; exit when it fails."""
    if subprocess.run(command, env=env, check=False).returncode:  # noqa: S603 - fixed commands
        raise SystemExit(f"{what} failed")


def setup(env: dict[str, str], *, reset: bool) -> None:
    if ensure_database(env[DATABASE_ENV]):
        print("Created the demo database.", flush=True)
    manage = [sys.executable, "-m", "demo.backend.manage"]
    run([*manage, "migrate", "--verbosity", "0"], env, "manage.py migrate")
    run([*manage, "seed_library", *(["--reset"] if reset else [])], env, "manage.py seed_library")
    if subprocess.run([sys.executable, "-m", "adorm", *GENERATE, "--check"], env=env, check=False).returncode:  # noqa: S603
        raise SystemExit(
            "demo/models is not up to date; regenerate it with\n"
            "  uv run adorm generate --settings demo.backend.settings -o demo/models --line-length 120"
        )
    check = [sys.executable, "-m", "adorm", "check-schema", "--settings", "demo.backend.settings", "-m", "demo.models"]
    run(check, env, "adorm check-schema")


def spawn(command: list[str], env: dict[str, str]) -> subprocess.Popen:
    """Start a child in its own session, so that Ctrl-C reaches it only through this process."""
    return subprocess.Popen(command, env=env, start_new_session=True)  # noqa: S603 - fixed commands


def stop(process: subprocess.Popen, label: str) -> None:
    """SIGTERM a child's process group (``npm run`` is a shell whose child holds the port), SIGKILL after the grace
    period."""
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    try:
        process.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        print(f"{label} did not stop in {STOP_GRACE_SECONDS}s; killing it", flush=True)
        with suppress(ProcessLookupError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.wait()


def _interrupt(*_args) -> None:
    """Turn SIGTERM into KeyboardInterrupt, so that the children are stopped."""
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--frontend",
        choices=["dev", "built", "none"],
        default="dev",
        help="dev: start the Vite dev server; built: build the frontend when demo/frontend/dist is missing and serve "
        "it from the API; none: the API only (default: dev)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="port of the API (default: 8000)")
    parser.add_argument("--frontend-port", type=int, default=5173, help="port of the dev server (default: 5173)")
    parser.add_argument("--database", help="database name in place of the one ADORM_DEMO_DATABASE_URL names")
    parser.add_argument("--reset", action="store_true", help="replace the demo data with the fixed data set")
    parser.add_argument("--no-setup", action="store_true", help="skip database setup")
    parser.add_argument("--setup-only", action="store_true", help="set up the database and exit")
    parser.add_argument("--drop", action="store_true", help="drop the demo database and exit")
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
    url = env.get(DATABASE_ENV, "")
    if args.database and url:
        url = env[DATABASE_ENV] = with_database(url, args.database)
    try:
        check_url(url)
        if args.drop:
            drop_database(url)
            return 0
        if not args.no_setup:
            setup(env, reset=args.reset)
    except DatabaseSetupError as exc:
        parser.error(str(exc))
    if args.setup_only:
        return 0
    if not drivers.available():
        parser.error("no driver is available: install adorm[django] or adorm[asyncpg]")

    children: list[tuple[str, subprocess.Popen]] = []
    if args.frontend == "dev":
        frontend_env = {**env, "DEMO_API_PORT": str(args.port), "DEMO_FE_PORT": str(args.frontend_port)}
        children.append(("the frontend", spawn(["npm", "run", "demo:dev"], frontend_env)))  # noqa: S607
        url = f"http://{args.host}:{args.frontend_port}/"
    else:
        url = f"http://{args.host}:{args.port}/docs"
    if args.frontend == "built":
        if not (FRONTEND_DIST / "index.html").is_file():
            run(["npm", "run", "demo:build"], env, "npm run demo:build")  # noqa: S607
        env["ADORM_DEMO_FRONTEND"] = str(FRONTEND_DIST)
        url = f"http://{args.host}:{args.port}/"

    os.environ.update(env)
    signal.signal(signal.SIGTERM, _interrupt)
    print(f"Drivers: {', '.join(driver.name for driver in drivers.available())}")
    print(f"Demo:    {url}", flush=True)
    import uvicorn

    try:
        uvicorn.run("demo.api.main:app", host=args.host, port=args.port)
    except KeyboardInterrupt:
        pass
    finally:
        for label, process in children:
            stop(process, label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
