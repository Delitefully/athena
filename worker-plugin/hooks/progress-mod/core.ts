// The progress band's pure parts: what workers/<name>.progress.json says, and the one line drawn from it.

/** What athena's progress hook writes (athena/caps/progress.py). */
export type Progress = {
  done: number
  total: number
  step: string
  source: string
  ts: number
  tasksTs: number
  stepTs: number
  /** When this task list began; 0 when unknown (an estimate, or a file from before list_ts). */
  listTs: number
}

/** One stretch of the main loop's turn as the mod saw it, epoch seconds; no end while the turn runs. */
export type Span = { start: number; end?: number }

/** What the mod knows of the worker right now, besides its progress file. */
export type Live = {
  /** The worker's state from its status file: working, idle, done, blocked... */
  state: string | undefined
  /** In a main-loop turn now, as the mod saw it start and not yet end. */
  inTurn: boolean
  /** A list last written before this, epoch seconds, belongs to finished work: the worker reported done after it, or
   *  the conversation was compacted while idle. */
  closedAt: number
  /** The turns' stretches this session, oldest first: the only time that counts as working. */
  spans: readonly Span[]
}

/** The band's line in parts, so the drawing can colour each. `age` and `eta` are '' when there is nothing true to say. */
export type Band =
  | { kind: 'progress'; bar: string; count: string; step: string; age: string; eta: string }
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
    listTs: num(o.list_ts),
  }
}

/** `▰▰▱▱▱` for done of total, BAR_CELLS wide. */
export function bar(done: number, total: number, cells = BAR_CELLS): string {
  const lit = total > 0 ? Math.max(0, Math.min(cells, Math.round((done / total) * cells))) : 0
  return '▰'.repeat(lit) + '▱'.repeat(cells - lit)
}

/** `0m`, `4m`, `1h05m`: the band itself never draws a `0m`. */
export function ageText(seconds: number): string {
  const minutes = Math.max(0, Math.floor(seconds / 60))
  return minutes < 60 ? `${minutes}m` : `${Math.floor(minutes / 60)}h${String(minutes % 60).padStart(2, '0')}m`
}

/** The seconds of working time from `since` to `now`: only the spans' stretches, never the idle gaps between. */
export function worked(spans: readonly Span[], since: number, now: number): number {
  let total = 0
  for (const span of spans) {
    const from = Math.max(span.start, since)
    const to = Math.min(span.end ?? now, now)
    if (to > from) total += to - from
  }
  return total
}

/** `eta 15m` from the list's own pace (working time since it began, per task done, times the tasks left); '' when
 *  there is no real value: an estimated count, no list start, nothing done yet, or under a minute of work to go on. */
export function eta(progress: Pick<Progress, 'done' | 'total' | 'source' | 'listTs'>, spans: readonly Span[], now: number): string {
  if (progress.source !== 'tasks' || progress.listTs <= 0 || progress.done < 1 || progress.done >= progress.total) return ''
  const spent = worked(spans, progress.listTs, now)
  if (spent < 60) return ''
  const left = (spent / progress.done) * (progress.total - progress.done)
  return `eta ${ageText(Math.ceil(left / 60) * 60)}`
}

/** The band: the bar, the count, the step, the working time on it and the eta, but only while the worker is in a turn
 *  on a live list. A turn that ended, a finished list (all done: Claude Code clears it) or one from before the work
 *  closed shows the worker's state alone; nothing before either. */
export function band(progress: Progress | undefined, live: Live, nowSeconds: number): Band | undefined {
  const current = progress && live.inTurn && progress.done < progress.total && progress.ts > live.closedAt
  if (progress && current) {
    const spent = worked(live.spans, progress.stepTs, nowSeconds)
    return {
      kind: 'progress',
      bar: bar(progress.done, progress.total),
      count: `${progress.done}/${progress.total}`,
      step: progress.step,
      age: spent >= 60 ? ageText(spent) : '',
      eta: eta(progress, live.spans, nowSeconds),
    }
  }
  return live.state ? { kind: 'state', state: live.state } : undefined
}

/** The band as plain text, as a pane shows it. */
export function bandLine(b: Band): string {
  if (b.kind === 'state') return b.state
  return [`${b.bar} ${b.count}`, b.step, b.age, b.eta].filter(Boolean).join(' · ')
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
