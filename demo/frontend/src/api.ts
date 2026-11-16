/** Types and calls of the demo API (`demo/api/main.py`). */

export interface DriverInfo {
  name: string;
  description: string;
  available: boolean;
  reason: string | null;
  mode: 'threaded' | 'async' | null;
}

export interface ParamInfo {
  name: string;
  label: string;
  kind: 'text' | 'int' | 'choice';
  default: string | number;
  choices: (string | number)[];
  minimum: number | null;
  maximum: number | null;
}

export interface ExampleInfo {
  id: string;
  title: string;
  description: string;
  code: string;
  params: ParamInfo[];
  writes: boolean;
}

export interface Statement {
  session: string | null;
  sql: string;
  params: unknown[];
  at_ms: number;
  duration_ms: number;
  error: string | null;
}

export interface ErrorOut {
  status: 'error';
  error: string;
  sqlstate: string | null;
  message: string;
  held_sessions: string[] | null;
}

export interface FlowOk {
  status: 'ok';
  result: unknown;
}

export interface FlowCancelled {
  status: 'cancelled';
  statement: string;
}

export type FlowOutcome = FlowOk | ErrorOut | FlowCancelled;

export interface ExampleRun {
  driver: string;
  ms: number;
  result: unknown;
  error: ErrorOut | null;
  statements: Statement[];
}

export interface ExampleRuns {
  example: string;
  params: Record<string, unknown>;
  runs: ExampleRun[];
}

export interface ScenarioInfo {
  id: string;
  title: string;
  description: string;
  code: string;
  choreography: string;
}

export interface FlowEvent {
  at_ms: number;
  flow: string;
  kind: 'info' | 'ok' | 'error' | 'observe';
  text: string;
}

export interface ScenarioRun {
  id: string;
  driver: string;
  ms: number;
  flows: string[];
  outcomes: Record<string, FlowOutcome>;
  events: FlowEvent[];
  statements: Statement[];
}

export interface ScenarioRuns {
  scenario: string;
  runs: ScenarioRun[];
}

async function call<T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const data = await response.json();
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
    } catch {
      // the body is not JSON
    }
    throw new Error(`${response.status}: ${detail}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  drivers: () => call<DriverInfo[]>('GET', '/api/drivers'),
  examples: () => call<ExampleInfo[]>('GET', '/api/examples'),
  runExample: (id: string, params: Record<string, unknown>) =>
    call<ExampleRuns>('POST', `/api/examples/${id}/run`, { params }),
  scenarios: () => call<ScenarioInfo[]>('GET', '/api/scenarios'),
  runScenario: (id: string) => call<ScenarioRuns>('POST', `/api/scenarios/${id}/run`),
};
