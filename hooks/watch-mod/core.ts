// The athena-watch mod's logic, free of the engine so it is tested on its own: reading `athena watch --tagged`
// output, and turning its actionable lines into as few HQ prompts as possible.

export type WatchRecord = { line: string; wake: boolean } | { status: string; board?: unknown }

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
      if (typeof v.status === 'string') return { status: v.status, board: v.board }
      if (typeof v.line === 'string') return { line: v.line, wake: v.wake === true }
    }
  } catch {
    // not JSON: fall through
  }
  return { line, wake: true }
}

export const MAX_LINES = 8
const MAX_LINE = 200

/**
 * One prompt for a batch: a stamped header, then one event per line. The model reads it as is; the HQ transcript
 * draws it as athena's own row (parseWake reads it back).
 *
 *     athena watch 22:19:
 *     plt-4041 report DONE
 *     pr6519 checks pending->failure
 */
export function promptText(lines: readonly string[], at?: string): string {
  const shown = lines.slice(0, MAX_LINES).map(l => (l.length > MAX_LINE ? l.slice(0, MAX_LINE - 1) + '…' : l))
  const more = lines.length - shown.length
  if (more > 0) shown.push(`+${more} more (athena status)`)
  return `athena watch${at ? ` ${at}` : ''}:\n${shown.join('\n')}`
}

const HEADER = /^athena watch(?: (\d{1,2}:\d{2}))?:\s*(.*)$/
const FRAME = /^(The \S+ plugin sent a message|This is how Claude Code surfaces)/

/** A wake prompt read back from a transcript row: its stamp and events. Tolerates the engine's frame around it and
 *  the one-line `athena watch: a · b` prompts sent before the stamp. Undefined for anything else. */
export function parseWake(text: string): { time: string | undefined; events: string[] } | undefined {
  const lines = text.split('\n')
  const at = lines.findIndex(l => HEADER.test(l.trim()))
  if (at < 0) return undefined
  const [, time, rest] = HEADER.exec(lines[at]!.trim())!
  const events = rest ? rest.split(' · ') : []
  for (const raw of lines.slice(at + 1)) {
    const line = raw.trim()
    if (FRAME.test(line)) break
    if (line) events.push(line)
  }
  return events.length ? { time, events } : undefined
}

/** `HH:MM` of a moment, in the zone `offsetMinutes` east of UTC (the module's clock reads UTC). */
export function clockText(ms: number, offsetMinutes: number): string {
  const d = new Date(ms + offsetMinutes * 60000)
  return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
}

// The words a wake may carry. HQ reads a wake as the person's own words (it is submitted asUser), so no text a
// worker, GitHub or a PR controls may reach it: every line is rebuilt from these, and anything else is held back.
const NAME = '[a-z][a-z0-9_-]{0,31}' // as paths.NAME_RE
const STATE = '(?:working|idle|done|blocked|exited|starting|unknown)'
const REPORT = '(?:DONE|DONE_WITH_CONCERNS|BLOCKED|NEEDS_CONTEXT)'
const CHECK = '(?:none|pending|success|failure)'
const EXIT = '(?:code -?\\d{1,3}|signal SIG[A-Z0-9]{1,10})'

/** Lines allowed as they are, each regex anchored. */
const EXACT = [
  `${NAME} report ${REPORT}`,
  `${NAME} state ${STATE}->${STATE}(?: \\(startup prompt\\))?`,
  `${NAME} pr (?:merged|closed|open)`,
  `${NAME} checks ${CHECK}->${CHECK}`,
  `${NAME} review (?:CHANGES_REQUESTED|APPROVED|REVIEW_REQUIRED)`,
  `${NAME} review comments \\+\\d{1,4}`,
  `[A-Za-z0-9._-]{1,64} main moved [0-9a-f]{7}\\.\\.[0-9a-f]{7}`,
  `${NAME} gone from the ledger`,
  `${NAME} new worker \\(${STATE}(?:, startup prompt)?\\)`,
  `${NAME} goal sent`,
  `watch error: exited \\(${EXIT}\\); restarting`,
].map(r => new RegExp(`^${r}$`))

/** Lines that carry free text, reduced to their allowlisted part. */
const REDUCED: [RegExp, (m: RegExpExecArray) => string][] = [
  [new RegExp(`^(${NAME}) pr opened https://github\\.com/[\\w.-]+/[\\w.-]+/pull/(\\d{1,7})$`), m => `${m[1]} pr opened #${m[2]}`],
  [
    /^stack [\w.-]+\/[\w.-]+#(\d{1,7}): \S.* -> (OPEN|MERGED|CLOSED) base=\S+(?: DRAFT)? (MERGEABLE|CONFLICTING|UNKNOWN)$/,
    m => (m[2] === 'OPEN' ? `stack #${m[1]}: OPEN ${m[3]}` : `stack #${m[1]}: ${m[2]}`),
  ],
  [new RegExp(`^(${NAME}) goal not sent: .+$`), m => `${m[1]} goal not sent`],
  [
    new RegExp(`^(${NAME}) goal unconfirmed \\(.*\\): check its pane, do not resend blindly$`),
    m => `${m[1]} goal unconfirmed: check its pane, do not resend blindly`,
  ],
  [new RegExp(`^watch error: exited \\((${EXIT})\\): .*; restarting$`), m => `watch error: exited (${m[1]}); restarting`],
  [/^watch error\b.*$/, () => 'watch error (athena status)'],
]

/** What a wake says in place of a line it may not carry, so HQ still looks. */
export const HELD = 'a watch line not shown here (athena status)'

/** A watch line in the words a wake may carry, or undefined when it holds anything else. */
export function allowLine(line: string): string | undefined {
  if (/[\r\n]/.test(line)) return undefined
  if (EXACT.some(r => r.test(line))) return line
  for (const [r, rebuild] of REDUCED) {
    const m = r.exec(line)
    if (m) return rebuild(m)
  }
  return undefined
}

export type Timer = { cancel: () => void }

export type WakerOptions = {
  /** Submits one prompt; resolves once its turn has started (or it was refused). */
  submit: (text: string) => Promise<unknown>
  after: (ms: number, fn: () => void) => Timer
  now: () => number
  /** The `HH:MM` a prompt is stamped with. */
  stamp?: () => string
  /** Lines arriving within this window go out as one prompt. */
  batchMs?: number
  /** A line told within this window is not told again: the same worker and event wakes HQ once. */
  repeatMs?: number
  /** What was told, and when, from before a reload or an HQ restart. */
  sent?: Readonly<Record<string, number>>
  /** Called with what was told in the window each time it changes, to keep it across a reload. */
  remember?: (sent: Record<string, number>) => void
}

/**
 * Holds actionable lines and submits them as one prompt: after a short quiet window, never while HQ runs a turn
 * (they wait for its end, then go out together), and never while an earlier submit is still on its way. Each line
 * is rebuilt from allowLine's words first (one it cannot rebuild goes out as HELD); a line already told in the last
 * ten minutes is dropped, so a worker that ends blocked twice wakes HQ once.
 */
export class Waker {
  private pending: string[] = []
  /** What each pending line is remembered by: the line, or a held line's own raw text. */
  private keys: string[] = []
  private timer: Timer | undefined
  private isBusy = false
  private isSending = false
  private told: Map<string, number>
  private readonly batchMs: number
  private readonly repeatMs: number

  constructor(private readonly opts: WakerOptions) {
    this.batchMs = opts.batchMs ?? 3000
    this.repeatMs = opts.repeatMs ?? 10 * 60 * 1000
    this.told = new Map(Object.entries(opts.sent ?? {}))
  }

  add(raw: string): void {
    const safe = allowLine(raw)
    const line = safe ?? HELD
    // A held line is remembered by its raw text (kept here, never sent), so one unknown line is told once too.
    const key = safe ?? `held:${raw.slice(0, 500)}`
    const last = this.told.get(key)
    if (last !== undefined && this.opts.now() - last < this.repeatMs) return
    if (!this.keys.includes(key)) this.keys.push(key)
    if (!this.pending.includes(line)) this.pending.push(line)
    this.schedule()
  }

  /**
   * A routine line: the worker (or stacked PR) it names moved on, so what was told about it may be told again. A
   * check that fails, goes pending after a fix and fails again wakes HQ twice; a replay with no change between, once.
   */
  settle(raw: string): void {
    const name = new RegExp(`^(${NAME}) `).exec(raw)?.[1]
    const pr = /^stack [\w.-]+\/[\w.-]+#(\d{1,7}):/.exec(raw)?.[1]
    const prefix = name ? `${name} ` : pr ? `stack #${pr}:` : undefined
    if (!prefix) return
    const before = this.told.size
    for (const key of [...this.told.keys()]) if (key.startsWith(prefix)) this.told.delete(key)
    if (this.told.size !== before) this.opts.remember?.(Object.fromEntries(this.told))
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

  private mark(keys: readonly string[], at: number | undefined): void {
    const now = this.opts.now()
    for (const [key, when] of this.told) if (now - when >= this.repeatMs) this.told.delete(key)
    for (const key of keys) {
      if (at === undefined) this.told.delete(key)
      else this.told.set(key, at)
    }
    this.opts.remember?.(Object.fromEntries(this.told))
  }

  private flush(): void {
    if (this.isBusy || this.isSending || !this.pending.length) return
    const lines = this.pending
    const keys = this.keys
    this.pending = []
    this.keys = []
    this.isSending = true
    // Told from the moment it is sent: a repeat arriving while the submit waits for its turn is dropped too.
    this.mark(keys, this.opts.now())
    this.opts
      .submit(promptText(lines, this.opts.stamp?.()))
      .catch(() => {
        this.mark(keys, undefined)
        this.pending = [...lines, ...this.pending.filter(l => !lines.includes(l))]
        this.keys = [...keys, ...this.keys.filter(k => !keys.includes(k))]
      })
      .finally(() => {
        this.isSending = false
        if (this.pending.length) this.schedule()
      })
  }
}

/** The status line's parts as `athena watch --tagged` reports them, most urgent first. */
export type Board = {
  /** `<worker> blocked`, `<worker> needs context`, `<worker> exited` */
  needs: string[]
  /** `#n` per PR waiting on the human */
  prs: string[]
  /** [state, workers] per active state: working, idle, starting */
  active: [string, string[]][]
  done: string[]
  /** `owner/repo` per PR number the watch knows from the ledger: a live worker's repo, or a stack's. */
  links?: Record<string, string>
  /** `owner/repo` per live worker, from its repo's origin remote. */
  repos?: Record<string, string>
  /** The local dashboard's URL while it runs, as `athena dash show` reads it. */
  dash?: string
}

export type Segment = { text: string; tone: 'needs' | 'active' | 'done' | 'dash'; href?: string }

/** One run of drawn text: plain, or a masked link to `href`. */
export type Part = { text: string; href?: string }

const SLUG = /^[A-Za-z0-9_.-]{1,100}\/[A-Za-z0-9_.-]{1,100}$/

/**
 * A PR's page, built only from an `owner/repo` the watch read from the ledger (never from worker, GitHub or PR
 * text) and a number. Undefined when either is not one, so the PR draws as plain `#n`.
 */
export function prUrl(repo: string | undefined, n: string | number): string | undefined {
  const num = String(n)
  if (!repo || !SLUG.test(repo) || !/^\d{1,7}$/.test(num)) return undefined
  return `https://github.com/${repo}/pull/${num}`
}

/** `owner/repo` of a GitHub remote, https or ssh; undefined for any other remote. */
export function repoSlug(remote: string): string | undefined {
  const m = /^(?:https:\/\/github\.com\/|git@github\.com:|ssh:\/\/git@github\.com\/)([\w.-]+\/[\w.-]+?)(?:\.git)?\/?$/.exec(remote.trim())
  return m && SLUG.test(m[1]!) ? m[1] : undefined
}

/** A drawn text split around each `#n`, each with its link where `href` knows one. */
export function linkParts(text: string, href: (n: string) => string | undefined): Part[] {
  const parts: Part[] = []
  let at = 0
  for (const m of text.matchAll(/#(\d{1,7})\b/g)) {
    if (m.index! > at) parts.push({ text: text.slice(at, m.index) })
    const url = href(m[1]!)
    parts.push(url ? { text: m[0], href: url } : { text: m[0] })
    at = m.index! + m[0].length
  }
  if (at < text.length || !parts.length) parts.push({ text: text.slice(at) })
  return parts
}

/** A PR number's link on the band: by the board's links, plain when it knows none. */
export function boardHref(b: Board | undefined): (n: string) => string | undefined {
  return n => prUrl(b?.links?.[n], n)
}

/** A wake event's parts: a PR it names links by the worker the event is about, else by the board's links. */
export function eventParts(event: string, b: Board | undefined): Part[] {
  // `stack #n:` and `watch error` lines name no worker, whatever workers are called.
  const worker = /^(?!stack #|watch error)([a-z][a-z0-9_-]{0,31}) /.exec(event)?.[1]
  const repo = worker ? b?.repos?.[worker] : undefined
  return linkParts(event, n => prUrl(repo ?? b?.links?.[n], n))
}

/** FORCE_HYPERLINK, then the terminals Claude Code 2.1.296 draws OSC 8 links for (else it adds the raw URL). */
const LINKING = ['ghostty', 'Hyper', 'kitty', 'alacritty', 'iTerm.app', 'iTerm2', 'WarpTerminal', 'WezTerm', 'vscode']

/**
 * Whether the engine draws a `Link` as a masked OSC 8 span: a mirror of Claude Code's own check, read from the same
 * variables. Where it does not (herdr unless FORCE_HYPERLINK is set), the mod draws plain text instead of the
 * engine's `label URL` fallback. A wrong answer costs little: false draws plain text, true the engine's fallback.
 */
export function hyperlinks(env: Readonly<Record<string, string | undefined>>): boolean {
  const force = env.FORCE_HYPERLINK
  if (force) return parseInt(force, 10) !== 0 // empty counts as unset, as in the engine
  const program = env.TERM_PROGRAM
  if (program && LINKING.includes(program)) return true
  if (env.LC_TERMINAL && LINKING.includes(env.LC_TERMINAL)) return true
  if (env.TERM?.includes('kitty') || env.TERM === 'alacritty') return true
  if (env.TERMINAL_EMULATOR === 'JetBrains-JediTerm') return true
  if (env.WT_SESSION && program !== 'tmux' && !env.TMUX) return true
  if (program === 'tmux') {
    const [major, minor] = (env.TERM_PROGRAM_VERSION ?? '').split('.').map(x => parseInt(x, 10))
    if (major! > 3 || (major === 3 && minor! >= 4)) return true
  }
  if (env.VTE_VERSION && env.VTE_VERSION !== '0.50.0') {
    const v = env.VTE_VERSION
    const minor = /^\d{3,4}$/.test(v) ? parseInt(v.slice(0, -2), 10) : parseInt(v.split('.')[1] ?? '', 10)
    const major = /^\d{3,4}$/.test(v) ? 0 : parseInt(v.split('.')[0] ?? '', 10)
    if (major > 0 || minor >= 50) return true
  }
  return false
}

/** The variables `hyperlinks` reads. */
export const LINK_ENV = [
  'FORCE_HYPERLINK',
  'TERM_PROGRAM',
  'TERM_PROGRAM_VERSION',
  'LC_TERMINAL',
  'TERM',
  'TERMINAL_EMULATOR',
  'WT_SESSION',
  'TMUX',
  'VTE_VERSION',
] as const

export const DASH_LINK = 'open dash →'

/** Width of the ` · ` between segments. */
export const GAP = 3

function needsCounted(b: Board): string[] {
  const kinds = new Map<string, number>()
  for (const item of b.needs) {
    const kind = item.slice(item.indexOf(' ') + 1)
    kinds.set(kind, (kinds.get(kind) ?? 0) + 1)
  }
  return [...kinds].map(([kind, n]) => `${n} ${kind}`)
}

function prsNamed(b: Board): string[] {
  return b.prs.length ? [`${b.prs.join(' ')} ${b.prs.length === 1 ? 'needs' : 'need'} you`] : []
}

function prsCounted(b: Board): string[] {
  return b.prs.length ? [b.prs.length === 1 ? '1 PR needs you' : `${b.prs.length} PRs need you`] : []
}

/**
 * The status line, laid out for `columns`: what needs the human, then the active workers, then the done ones,
 * each named while the line fits, then the dash link. As it narrows, the dash link goes first, then the done ones
 * fold to a count, then the active ones, then the PRs, then the needs; whatever is still too wide is cut by the drawing.
 */
export function fitStatus(b: Board, columns: number): Segment[] {
  const dash: Segment[] = b.dash ? [{ text: DASH_LINK, tone: 'dash', href: b.dash }] : []
  if (!b.needs.length && !b.prs.length && !b.active.length && !b.done.length) {
    return [{ text: 'no live workers', tone: 'active' }, ...dash]
  }
  const activeNamed = b.active.map(([state, names]) => `${names.join(', ')} ${state}`)
  const activeCounted = b.active.map(([state, names]) => `${names.length} ${state}`)
  const doneNamed = b.done.length ? [`✓ ${b.done.join(', ')} done`] : []
  const doneCounted = b.done.length ? [`✓ ${b.done.length} done`] : []
  const levels: [string[], string[], string[], string[]][] = [
    [b.needs, prsNamed(b), activeNamed, doneNamed],
    [b.needs, prsNamed(b), activeNamed, doneCounted],
    [b.needs, prsNamed(b), activeCounted, doneCounted],
    [b.needs, prsCounted(b), activeCounted, doneCounted],
    [needsCounted(b), prsCounted(b), activeCounted, doneCounted],
  ]
  // The dash link always ends the line.
  const segments = ([needs, prs, active, done]: [string[], string[], string[], string[]]): Segment[] => {
    const urgent = [...needs, ...prs].map(text => ({ text, tone: 'needs' as const }))
    const rest = [...active.map(text => ({ text, tone: 'active' as const })), ...done.map(text => ({ text, tone: 'done' as const }))]
    return [...urgent, ...rest, ...dash]
  }
  const width = (s: Segment[]) => s.reduce((n, x) => n + x.text.length, 0) + GAP * (s.length - 1)
  // Names and `#n` outrank the dash link: each fold is tried with it, then without it, before folding further.
  for (const level of levels) {
    const s = segments(level)
    if (width(s) <= columns) return s
    const bare = s.filter(x => x.tone !== 'dash')
    if (bare.length < s.length && width(bare) <= columns) return bare
  }
  return segments(levels[levels.length - 1]!).filter(s => s.tone !== 'dash')
}

/** Reads a `board` record, or undefined when it is not one. */
export function readBoard(value: unknown): Board | undefined {
  if (!value || typeof value !== 'object') return undefined
  const v = value as Record<string, unknown>
  const strings = (x: unknown) => (Array.isArray(x) ? x.filter((s): s is string => typeof s === 'string') : [])
  const active = Array.isArray(v.active)
    ? v.active.flatMap(g => (Array.isArray(g) && typeof g[0] === 'string' ? [[g[0], strings(g[1])] as [string, string[]]] : []))
    : []
  const map = (x: unknown, key: RegExp) =>
    x && typeof x === 'object' && !Array.isArray(x)
      ? Object.fromEntries(Object.entries(x).filter((kv): kv is [string, string] => key.test(kv[0]) && typeof kv[1] === 'string' && SLUG.test(kv[1])))
      : {}
  const dash = typeof v.dash === 'string' && /^http:\/\/(?:127\.0\.0\.1|localhost):\d{1,5}\/$/.test(v.dash) ? v.dash : undefined
  return {
    needs: strings(v.needs),
    prs: strings(v.prs),
    active,
    done: strings(v.done),
    links: map(v.links, /^\d{1,7}$/),
    repos: map(v.repos, /^[a-z][a-z0-9_-]{0,31}$/),
    dash,
  }
}

/** How long to wait before starting the watch again: doubles per quick exit, from 5 s up to a minute. */
export function restartDelay(quickExits: number): number {
  return Math.min(5000 * 2 ** Math.max(0, quickExits - 1), 60000)
}

/** A Monitor task's notification row: `Monitor event: ...`, `Monitor "<name>" stream ended`, its expiry. */
export function isMonitorNotice(text: string): boolean {
  return /^\[?Monitor (event:|")/.test(text) || /^\[?Monitor expired\b/.test(text)
}

/** A worker's ATHENA-REPORT, as its final message carries it. */
export type Report = {
  status: string
  pr?: string
  head?: string
  verify?: string
  decisions: string[]
  concerns: string[]
}

const REPORT_LINE = /^`?ATHENA-REPORT\s+(\{.*\})`?$/
const FENCE = /^```\w*$/

/**
 * The ATHENA-REPORT line of a message, parsed, with the text before and after it (a code fence around the line
 * goes with it). Undefined when there is none or its JSON does not parse to an object with a status.
 */
export function splitReport(text: string): { before: string; after: string; report: Report } | undefined {
  const lines = text.split('\n')
  for (let i = lines.length - 1; i >= 0; i--) {
    const m = REPORT_LINE.exec(lines[i]!.trim())
    if (!m) continue
    let value: unknown
    try {
      value = JSON.parse(m[1]!)
    } catch {
      return undefined
    }
    if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
    const v = value as Record<string, unknown>
    if (typeof v.status !== 'string') return undefined
    const str = (x: unknown) => (typeof x === 'string' ? x : undefined)
    const list = (x: unknown) => (Array.isArray(x) ? x.filter((s): s is string => typeof s === 'string') : [])
    const fenced = i > 0 && i < lines.length - 1 && FENCE.test(lines[i - 1]!.trim()) && lines[i + 1]!.trim() === '```'
    const from = fenced ? i - 1 : i
    const to = fenced ? i + 2 : i + 1
    return {
      before: lines.slice(0, from).join('\n').trim(),
      after: lines.slice(to).join('\n').trim(),
      report: {
        status: v.status,
        pr: str(v.pr),
        head: str(v.head),
        verify: str(v.verify),
        decisions: list(v.decisions),
        concerns: list(v.concerns),
      },
    }
  }
  return undefined
}

/** Needs context: an orange between the theme's warning and error. */
export const ORANGE = '#E8913A'

const LOOKS: Record<string, { text: string; color: string }> = {
  DONE: { text: 'Done', color: 'success' },
  DONE_WITH_CONCERNS: { text: 'Done with concerns', color: 'warning' },
  BLOCKED: { text: 'Blocked', color: 'error' },
  NEEDS_CONTEXT: { text: 'Needs context', color: ORANGE },
}

export type ReportView = {
  status: { text: string; color: string | undefined }
  pr: { href: string | undefined; label: string } | undefined
  head: string | undefined
  verify: string | undefined
  decisions: string[]
  concerns: string[]
}

/** C0 and C1 controls, and the bidi and zero-width characters that can make a line say one thing and show another. */
const UNSAFE = /[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]/g

/** A report field as it may be drawn: controls, bidi and zero-width characters removed. */
export function clean(text: string): string {
  return text.replace(UNSAFE, '')
}

/**
 * What the completion block shows: the status in its word and colour, the PR as `#n` with its head, the rest. The
 * PR links to its page in `repo`, the worker's own repo from its origin remote: the report's URL gives only the
 * number, and a URL into another repo draws as plain `owner/repo#n`.
 */
export function reportView(raw: Report, repo?: string): ReportView {
  const r: Report = {
    status: clean(raw.status),
    pr: raw.pr === undefined ? undefined : clean(raw.pr),
    head: raw.head === undefined ? undefined : clean(raw.head),
    verify: raw.verify === undefined ? undefined : clean(raw.verify),
    decisions: raw.decisions.map(clean),
    concerns: raw.concerns.map(clean),
  }
  const look = LOOKS[r.status.toUpperCase()] ?? { text: r.status, color: undefined }
  return {
    status: look,
    pr: r.pr ? prView(r.pr, repo) : undefined,
    head: r.head ? r.head.slice(0, 7) : undefined,
    verify: r.verify || undefined,
    decisions: r.decisions,
    concerns: r.concerns,
  }
}

function prView(pr: string, repo: string | undefined): { href: string | undefined; label: string } {
  const url = /^https:\/\/github\.com\/([\w.-]+\/[\w.-]+)\/pull\/(\d{1,7})\/?$/.exec(pr)
  const n = url?.[2] ?? /^#?(\d{1,7})$/.exec(pr)?.[1]
  if (!n) return { href: undefined, label: pr }
  if (url && url[1]!.toLowerCase() !== repo?.toLowerCase()) {
    return { href: undefined, label: repo ? `${url[1]}#${n}` : `#${n}` }
  }
  return { href: prUrl(repo, n), label: `#${n}` }
}
