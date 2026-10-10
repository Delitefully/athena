// The athena-watch mod's logic, free of the engine so it is tested on its own: reading `athena watch --tagged`
// output, and turning its actionable lines into as few HQ prompts as possible.

export type WatchRecord = { line: string; wake: boolean } | { status: string }

/** Splits what the child wrote into whole lines; a line that is not a record of ours is an actionable line as is. */
export function readRecords(rest: string, text: string): { records: WatchRecord[]; rest: string } {
  const parts = (rest + text).split('\n')
  const tail = parts.pop() ?? ''
  const records: WatchRecord[] = []
  for (const raw of parts) {
    const line = raw.trim()
    if (!line) continue
    records.push(parseRecord(line))
  }
  return { records, rest: tail }
}

function parseRecord(line: string): WatchRecord {
  try {
    const value: unknown = JSON.parse(line)
    if (value && typeof value === 'object') {
      const v = value as Record<string, unknown>
      if (typeof v.status === 'string') return { status: v.status }
      if (typeof v.line === 'string') return { line: v.line, wake: v.wake === true }
    }
  } catch {
    // not JSON: fall through
  }
  return { line, wake: true }
}

export const MAX_LINES = 8
const MAX_LINE = 200

/** One short prompt for a batch: `athena watch: plt-4041 report DONE · pr6519 checks pending->failure`. */
export function promptText(lines: readonly string[]): string {
  const shown = lines.slice(0, MAX_LINES).map(l => (l.length > MAX_LINE ? l.slice(0, MAX_LINE - 1) + '…' : l))
  const more = lines.length - shown.length
  return `athena watch: ${shown.join(' · ')}${more > 0 ? ` · +${more} more (athena status)` : ''}`
}

export type Timer = { cancel: () => void }

export type WakerOptions = {
  /** Submits one prompt; resolves once its turn has started (or it was refused). */
  submit: (text: string) => Promise<unknown>
  after: (ms: number, fn: () => void) => Timer
  now: () => number
  /** Lines arriving within this window go out as one prompt. */
  batchMs?: number
  /** The same watch error is told once in this window; the status line still shows it. */
  repeatMs?: number
}

/**
 * Holds actionable lines and submits them as one prompt: after a short quiet window, never while HQ runs a turn
 * (they wait for its end, then go out together), and never while an earlier submit is still on its way.
 */
export class Waker {
  private pending: string[] = []
  private timer: Timer | undefined
  private isBusy = false
  private isSending = false
  private told = new Map<string, number>()
  private readonly batchMs: number
  private readonly repeatMs: number

  constructor(private readonly opts: WakerOptions) {
    this.batchMs = opts.batchMs ?? 3000
    this.repeatMs = opts.repeatMs ?? 10 * 60 * 1000
  }

  add(line: string): void {
    if (line.startsWith('watch error')) {
      const at = this.opts.now()
      const last = this.told.get(line)
      if (last !== undefined && at - last < this.repeatMs) return
      this.told.set(line, at)
    }
    if (!this.pending.includes(line)) this.pending.push(line)
    this.schedule()
  }

  turnStarted(): void {
    this.isBusy = true
  }

  turnEnded(): void {
    this.isBusy = false
    if (this.pending.length) this.schedule()
  }

  get waiting(): readonly string[] {
    return this.pending
  }

  private schedule(): void {
    if (this.timer) return
    this.timer = this.opts.after(this.batchMs, () => {
      this.timer = undefined
      this.flush()
    })
  }

  private flush(): void {
    if (this.isBusy || this.isSending || !this.pending.length) return
    const lines = this.pending
    this.pending = []
    this.isSending = true
    this.opts
      .submit(promptText(lines))
      .catch(() => {
        this.pending = [...lines, ...this.pending]
      })
      .finally(() => {
        this.isSending = false
        if (this.pending.length) this.schedule()
      })
  }
}

/** How long to wait before starting the watch again: doubles per quick exit, from 5 s up to a minute. */
export function restartDelay(quickExits: number): number {
  return Math.min(5000 * 2 ** Math.max(0, quickExits - 1), 60000)
}
