"""Runs, on every available driver, a scenario whose choreography raises while a flow holds a row lock, then the
``race`` scenario; prints one JSON line per driver. ``tests/demo/test_smoke.py`` runs it in a child process on the
demo database ``ADORM_DEMO_DATABASE_URL`` names."""

import asyncio
import json
import time

from demo.api.main import app, lifespan
from demo.api.scenarios import hold_edition, pause_at, RACE_ISBN, Run, run_scenario, Scenario, SCENARIOS

LIMIT = 20.0
"""Seconds a run may take before the probe reports it as hanging."""


async def fail_after_lock(run: Run) -> None:
    locked, go = asyncio.Event(), asyncio.Event()
    a = run.flow("A", hold_edition, RACE_ISBN, pause_at(locked, go))
    await run.until(locked, a)
    raise RuntimeError("the choreography failed")


FAILING = Scenario("failing", "Failing", "", fail_after_lock, (hold_edition,))


async def main() -> None:
    async with lifespan(app):
        for driver in app.state.drivers:
            started = time.monotonic()
            try:
                await asyncio.wait_for(run_scenario(FAILING, driver), LIMIT)
                error = None
            except Exception as exc:  # noqa: BLE001 - reported
                error = type(exc).__name__
            failed_ms = (time.monotonic() - started) * 1000
            flows = [task.get_name() for task in asyncio.all_tasks() if task.get_name().startswith("flow ")]
            race = await asyncio.wait_for(run_scenario(SCENARIOS["race"], driver), LIMIT)
            print(
                json.dumps(
                    {
                        "driver": driver,
                        "error": error,
                        "failed_ms": failed_ms,
                        "flows_left": flows,
                        "race": {flow: outcome["status"] for flow, outcome in race["outcomes"].items()},
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    asyncio.run(main())
