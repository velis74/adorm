"""Runs, on every available driver, the writing example ``ensure_tag`` and the scenario ``race`` and cancels each run
after its write: a number of loop iterations after the example function or the choreography returned or after the
scenario's snapshot of its data, and once when the example's ``undo`` starts. Prints one JSON line per run.
``tests/demo/test_smoke.py`` runs it in a child process on the demo database ``ADORM_DEMO_DATABASE_URL`` names."""

import asyncio
import dataclasses
import json

import adorm

from demo.api import scenarios
from demo.api.capture import CAPTURE
from demo.api.examples import EXAMPLES
from demo.api.main import app, lifespan, run_example
from demo.api.scenarios import RACE_ISBN, run_scenario, SCENARIOS
from demo.models.library import Edition, Loan, Tag

TAG = "cancelled-run"
HOPS = range(8)
"""Loop iterations between the end of the example function or the choreography and the cancellation."""
SETUP_HOPS = range(40)
"""Loop iterations between the snapshot of a scenario's data and the cancellation, which lands in the scenario's
setup write or after it."""
LIMIT = 20.0
"""Seconds a run may take before the probe reports it as hanging."""
RUNNING: dict[str, asyncio.Task] = {}
"""The task of the current run."""


def cancel_after(task: asyncio.Task, hops: int) -> None:
    """Cancel ``task`` ``hops`` loop iterations from now."""
    loop = asyncio.get_running_loop()
    if hops == 0:
        loop.call_soon(task.cancel)
    else:
        loop.call_soon(cancel_after, task, hops - 1)


async def outcome(run) -> str:
    """Run ``run()`` in a task of its own, ``RUNNING["task"]``, and return ``cancelled``, ``finished`` or
    ``hanging``."""
    task = asyncio.create_task(run())
    RUNNING["task"] = task
    try:
        await asyncio.wait_for(asyncio.shield(task), LIMIT)
    except asyncio.CancelledError:
        return "cancelled"
    except TimeoutError:
        task.cancel()
        return "hanging"
    return "finished"


async def tag_exists(driver: str) -> bool:
    with adorm.create_session(driver, "verify").activate():
        return await Tag.objects.filter(name=TAG).exists()


async def race_state(driver: str) -> dict:
    with adorm.create_session(driver, "verify").activate():
        edition = await Edition.objects.get(isbn=RACE_ISBN)
        return {"available": edition.available, "loans": await Loan.objects.filter(edition_id=edition.id).count()}


async def cleanup(driver: str) -> None:
    with adorm.create_session(driver, "verify").activate():
        await Tag.objects.filter(name=TAG).delete()


def report(**fields) -> None:
    fields["capture_active"] = CAPTURE._statements is not None  # noqa: SLF001 - no public accessor
    print(json.dumps(fields), flush=True)


async def example_runs(driver: str) -> None:
    original = EXAMPLES["ensure_tag"]
    for hops in HOPS:

        async def cancelled_after_write(_hops=hops, **params):
            result = await original.function(**params)
            cancel_after(RUNNING["task"], _hops)
            return result

        example = dataclasses.replace(original, function=cancelled_after_write)

        async def run(example=example):
            await run_example(example, driver, {"name": TAG})

        result = await outcome(run)
        report(kind="example", driver=driver, hops=hops, outcome=result, written=await tag_exists(driver))
        await CAPTURE.stop()
        await cleanup(driver)

    async def undo_cancelled(result, original_undo=original.undo):
        cancel_after(RUNNING["task"], 0)
        await original_undo(result)

    example = dataclasses.replace(original, undo=undo_cancelled)

    async def run():
        await run_example(example, driver, {"name": TAG})

    result = await outcome(run)
    report(kind="undo", driver=driver, hops=0, outcome=result, written=await tag_exists(driver))
    await CAPTURE.stop()
    await cleanup(driver)


async def scenario_runs(driver: str) -> None:
    original = SCENARIOS["race"]
    before = await race_state(driver)
    await run_scenario(original, driver)
    report(kind="baseline", driver=driver, hops=None, outcome="finished", restored=await race_state(driver) == before)
    for hops in HOPS:
        before = await race_state(driver)

        async def cancelled_after_choreography(run_, _hops=hops):
            await original.choreography(run_)
            cancel_after(RUNNING["task"], _hops)

        scenario = dataclasses.replace(original, choreography=cancelled_after_choreography)

        async def run(scenario=scenario):
            await run_scenario(scenario, driver)

        result = await outcome(run)
        flows = [task.get_name() for task in asyncio.all_tasks() if task.get_name().startswith("flow ")]
        report(
            kind="scenario",
            driver=driver,
            hops=hops,
            outcome=result,
            restored=await race_state(driver) == before,
            flows_left=flows,
        )
        await CAPTURE.stop()
    original_snapshot = scenarios.snapshot
    for hops in SETUP_HOPS:
        before = await race_state(driver)

        async def cancelled_after_snapshot(_hops=hops):
            state = await original_snapshot()
            cancel_after(RUNNING["task"], _hops)
            return state

        async def run():
            await run_scenario(original, driver)

        scenarios.snapshot = cancelled_after_snapshot
        try:
            result = await outcome(run)
        finally:
            scenarios.snapshot = original_snapshot
        report(kind="setup", driver=driver, hops=hops, outcome=result, restored=await race_state(driver) == before)
        await CAPTURE.stop()


async def main() -> None:
    async with lifespan(app):
        for driver in app.state.drivers:
            await cleanup(driver)
            await example_runs(driver)
            await scenario_runs(driver)


if __name__ == "__main__":
    asyncio.run(main())
