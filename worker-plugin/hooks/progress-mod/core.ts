// The progress band's pure parts: what workers/<name>.progress.json says, and the one line drawn from it.

/** What athena-rex's progress hook writes (athena_rex/caps/progress.py). */
export type Progress = {
  done: number
  total: number
  step: string
  source: string
  ts: number
  tasksTs: number
  stepTs: number
}

/** The band's line in parts, so the drawing can colour each. */
export type Band =
  | { kind: 'progress'; bar: string; count: string; step: string; age: string; source: string }
  | { kind: 'state'; state: string }

export const BAR_CELLS = 5
/** The mod asks for a Haiku estimate no sooner than this after its session starts or its last ask. progress.py
 *  decides for good; this only spares a process start on every tool call. */
export const QUIET_MS = 600_000
const LINE_MAX = 200
const LINES = 40

function num(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/** A JSON object's fields, or undefined for anything else. */
export function parseObject(text: string | undefined): Record<string, unknown> | undefined {
  if (!text) return undefined
  try {
    const value: unknown = JSON.parse(text)
    return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : undefined
  } catch {
    return undefined
  }
}

export function parseProgress(text: string | undefined): Progress | undefined {
  const o = parseObject(text)
  if (!o || num(o.total) <= 0) return undefined
  return {
    done: Math.min(num(o.done), num(o.total)),
    total: num(o.total),
    step: str(o.step),
    source: str(o.source),
    ts: num(o.ts),
    tasksTs: num(o.tasks_ts),
    stepTs: num(o.step_ts) || num(o.ts),
  }
}

/** `▰▰▱▱▱` for done of total, BAR_CELLS wide. */
export function bar(done: number, total: number, cells = BAR_CELLS): string {
  const lit = total > 0 ? Math.max(0, Math.min(cells, Math.round((done / total) * cells))) : 0
  return '▰'.repeat(lit) + '▱'.repeat(cells - lit)
}

/** `0m`, `4m`, `1h05m`. */
export function ageText(seconds: number): string {
  const minutes = Math.max(0, Math.floor(seconds / 60))
  return minutes < 60 ? `${minutes}m` : `${Math.floor(minutes / 60)}h${String(minutes % 60).padStart(2, '0')}m`
}

/** The band: the bar, the count, the step and how long it has run, and where it came from; before any task list,
 *  the worker's state alone; nothing before either. */
export function band(progress: Progress | undefined, state: string | undefined, nowSeconds: number): Band | undefined {
  if (progress) {
    return {
      kind: 'progress',
      bar: bar(progress.done, progress.total),
      count: `${progress.done}/${progress.total}`,
      step: progress.step,
      age: ageText(nowSeconds - progress.stepTs),
      source: progress.source === 'estimate' ? 'estimate' : 'task list',
    }
  }
  return state ? { kind: 'state', state } : undefined
}

/** The band as plain text, as a pane shows it. */
export function bandLine(b: Band): string {
  if (b.kind === 'state') return b.state
  return [`${b.bar} ${b.count}`, b.step, b.age, b.source].filter(Boolean).join(' · ')
}

type Message = { role: string; text: string; toolUses?: readonly { tool: string; input: Record<string, unknown> }[] }

/** The transcript's last LINES lines, each cut to LINE_MAX: what the Haiku estimate reads. */
export function transcriptLines(messages: readonly Message[]): string[] {
  const lines: string[] = []
  for (const m of messages.slice(-LINES)) {
    for (const line of m.text.split('\n')) if (line.trim()) lines.push(`${m.role}: ${line}`.slice(0, LINE_MAX))
    for (const use of m.toolUses ?? []) lines.push(`tool ${use.tool}: ${JSON.stringify(use.input)}`.slice(0, LINE_MAX))
  }
  return lines.slice(-LINES)
}
