import { expect, test, type Locator, type Page } from '@playwright/test';

const DRIVERS = ['django', 'asyncpg'];

/** The drivers the API runs on. */
async function availableDrivers(page: Page): Promise<string[]> {
  const response = await page.request.get('/api/health');
  return (await response.json()).drivers as string[];
}

function run(page: Page, driver: string): Locator {
  return page.getByTestId(`run-${driver}`);
}

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('example-title')).toBeVisible();
});

test('the built frontend is served by the API', async ({ page }) => {
  const index = await page.request.get('/');
  expect(index.headers()['content-type']).toContain('text/html');
  const script = /src="\/(assets\/[^"]+\.js)"/.exec(await index.text());
  expect(script).not.toBeNull();
  expect((await page.request.get(`/${script![1]}`)).status()).toBe(200);
});

test('the header lists both drivers as available', async ({ page }) => {
  expect(await availableDrivers(page)).toEqual(DRIVERS);
  await expect(page.getByTestId('driver-django')).toContainText('threaded');
  await expect(page.getByTestId('driver-asyncpg')).toContainText('async');
});

test('the introduction is open on the first visit, closed on the next, and opens again', async ({ page }) => {
  await expect(page.getByTestId('intro-text')).toBeVisible();
  await expect(page.getByTestId('intro-text')).toContainText('django, asyncpg');
  await page.reload();
  await expect(page.getByTestId('intro-text')).toBeHidden();
  await page.getByTestId('intro-toggle').click();
  await expect(page.getByTestId('intro-text')).toBeVisible();
  await page.reload();
  await expect(page.getByTestId('intro-text')).toBeVisible();
});

test('the theme switches between light and dark and stays after a reload', async ({ page }) => {
  const app = page.locator('.v-application');
  const light = await app.evaluate((element) => element.classList.contains('v-theme--light'));
  await page.getByTestId('theme-toggle').click();
  await expect(app).toHaveClass(light ? /v-theme--dark/ : /v-theme--light/);
  await page.reload();
  await expect(app).toHaveClass(light ? /v-theme--dark/ : /v-theme--light/);
});

test('code and SQL are highlighted', async ({ page }) => {
  await expect(page.getByTestId('example-code').locator('.hljs-keyword').first()).toContainText(/async|def|await/);
  await page.getByTestId('run-example').click();
  const statement = run(page, (await availableDrivers(page))[0])
    .getByTestId('sql-statement')
    .first();
  await expect(statement.locator('.hljs-keyword').first()).toContainText(/SELECT|BEGIN/i);
});

test('every example runs on every driver with its code, SQL, result and time', async ({ page }) => {
  const drivers = await availableDrivers(page);
  const items = page.getByTestId('example-list').getByRole('listitem');
  const count = await items.count();
  expect(count).toBeGreaterThanOrEqual(10);
  for (let index = 0; index < count; index += 1) {
    await items.nth(index).click();
    await expect(page.getByTestId('example-code')).toContainText('async def');
    await page.getByTestId('run-example').click();
    for (const driver of drivers) {
      const card = run(page, driver);
      await expect(card.getByTestId('run-time')).toContainText('ms');
      await expect(card.getByTestId('run-error')).toHaveCount(0);
      await expect(card.getByTestId('sql-statement').first()).toBeVisible();
      await expect(card.getByTestId('result')).toBeVisible();
    }
  }
});

test('a parameter changes the query and its result', async ({ page }) => {
  await page.getByTestId('example-search_books').click();
  const search = page.getByTestId('param-search').locator('input');
  await search.fill('karamazov');
  await page.getByTestId('run-example').click();
  for (const driver of await availableDrivers(page)) {
    await expect(run(page, driver).getByTestId('result')).toContainText('The Brothers Karamazov');
    await expect(run(page, driver).getByTestId('sql-statement').first()).toContainText('%karamazov%');
  }
});

test('a write example shows its transaction and is reverted', async ({ page }) => {
  await page.getByTestId('example-lend').click();
  await page.getByTestId('run-example').click();
  for (const driver of await availableDrivers(page)) {
    const statements = run(page, driver).getByTestId('sql-statement');
    await expect(statements.first()).toContainText('BEGIN');
    await expect(statements.filter({ hasText: 'FOR UPDATE' })).toHaveCount(1);
    await expect(statements.last()).toContainText('COMMIT');
    await expect(run(page, driver).getByTestId('result')).toContainText('"available_before": 3');
  }
});

/** Runs a scenario and returns the status of each flow per driver. */
async function runScenario(page: Page, id: string): Promise<Record<string, Record<string, string>>> {
  await page.getByTestId('tab-scenarios').click();
  await page.getByTestId(`scenario-${id}`).click();
  await expect(page.getByTestId('scenario-code')).toContainText('async def');
  await page.getByTestId('run-scenario').click();
  await expect(page.getByTestId('scenario-runs')).toBeVisible({ timeout: 30_000 });
  const statuses: Record<string, Record<string, string>> = {};
  for (const driver of await availableDrivers(page)) {
    const card = run(page, driver);
    await expect(card.getByTestId('timeline')).toBeVisible();
    statuses[driver] = {};
    for (const chip of await card.locator('[data-testid^="outcome-"]').all()) {
      const flow = (await chip.getAttribute('data-testid'))!.replace('outcome-', '');
      statuses[driver][flow] = (await chip.getAttribute('data-status'))!;
    }
  }
  return statuses;
}

test('two flows race for one row: one lends the copy, the other finds none left', async ({ page }) => {
  for (const flows of Object.values(await runScenario(page, 'race'))) {
    expect(flows).toEqual({ A: 'ok', B: 'error' });
  }
  for (const driver of await availableDrivers(page)) {
    await expect(run(page, driver).getByTestId('event-demo').first()).toContainText('B waits for a lock');
    await expect(run(page, driver).getByTestId('outcome-B')).toContainText('LookupError');
  }
});

test('a lock wait ends with the lock timeout', async ({ page }) => {
  for (const flows of Object.values(await runScenario(page, 'lock_timeout'))) {
    expect(flows).toEqual({ A: 'ok', B: 'error' });
  }
  for (const driver of await availableDrivers(page)) {
    await expect(run(page, driver).getByTestId('outcome-B')).toContainText('55P03');
  }
});

test('cancelling a flow cancels its statement', async ({ page }) => {
  for (const flows of Object.values(await runScenario(page, 'cancel'))) {
    expect(flows).toEqual({ A: 'ok', B: 'cancelled' });
  }
});

test('the database detects the deadlock and one flow commits', async ({ page }) => {
  for (const [driver, flows] of Object.entries(await runScenario(page, 'deadlock'))) {
    expect(Object.values(flows).sort()).toEqual(['error', 'ok']);
    await expect(run(page, driver).getByText('40P01')).not.toHaveCount(0);
  }
});

test('adorm detects a deadlock on the slots at once', async ({ page }) => {
  for (const [driver, flows] of Object.entries(await runScenario(page, 'slot_deadlock'))) {
    expect(flows).toEqual({ A: 'ok', B: 'error' });
    await expect(run(page, driver).getByTestId('outcome-B')).toContainText('DeadlockError, holding B');
  }
});

test('an exhausted pool times out the flow waiting for a slot', async ({ page }) => {
  for (const [driver, flows] of Object.entries(await runScenario(page, 'pool_exhaustion'))) {
    expect(flows).toEqual({ A: 'ok', B: 'ok', C: 'error', D: 'ok' });
    await expect(run(page, driver).getByTestId('outcome-C')).toContainText('SessionTimeout');
  }
});
