// athena-watch: in the HQ session alone (ATHENA_ROLE=hq), runs `athena watch --tagged` for the session's life,
// draws a one-line status band above the prompt, and wakes HQ with one short prompt only for lines that need it,
// each event once in ten minutes. A wake draws as athena's own compact row, without the engine's plugin frame.
// It also hides any Monitor rows left in HQ's transcript (the call, its result and its notifications). In a worker
// (ATHENA_WORKER set), it draws the ATHENA-REPORT line as a completion block. Every other session sees no change.
import type { EngineInterface, Register } from 'claude-code'

import type { Board, Part, Segment } from './core'
import {
  boardHref,
  clockText,
  eventParts,
  fitStatus,
  hyperlinks,
  isMonitorNotice,
  LINK_ENV,
  linkParts,
  parseWake,
  readBoard,
  readRecords,
  repoSlug,
  reportView,
  restartDelay,
  splitReport,
  Waker,
} from './core'

/** A run shorter than this counts as a quick exit, and the next start waits longer. */
const STEADY_MS = 60000

/** athena's colour (docs/brand.md): lit madder, which reads on a dark ground and a light one. */
const MADDER = '#E0573D'
/** athena's mark in a terminal: one continuous looped thread, in any font. */
const MARK = '∞'
/** The store key for what was told in the last ten minutes, kept across a reload or an HQ restart; one per athena
 *  state dir, since every session running the plugin shares its store. */
async function toldKey($: EngineInterface): Promise<string> {
  return `told:${(await $.env.get('ATHENA_STATE_DIR')) || 'default'}`
}

let role: Promise<boolean> | undefined
let isWatching = false
let waker: Waker | undefined
/** What the band shows: the board from the last status record, or a note while the watch starts or restarts. */
let board: Board | undefined
let note: string | undefined
/** The last wake, drawn as athena's styled row at the top of the band until the person next types a prompt. */
let lastWake: { time: string | undefined; events: string[] } | undefined

/** The `env` of the settings files, or nothing where they cannot be read. */
async function settingsEnv($: EngineInterface): Promise<Record<string, unknown>> {
  const env = await $.settings.read().then(s => s.env as Record<string, unknown> | undefined, () => undefined)
  return env ?? {}
}

// HQ only: ATHENA_ROLE=hq, and never a worker, even one that inherited HQ's environment.
async function readRole($: EngineInterface): Promise<boolean> {
  const role = (await $.env.get('ATHENA_ROLE')) ?? (await settingsEnv($)).ATHENA_ROLE
  return role === 'hq' && !(await readWorker($))
}

let worker: Promise<boolean> | undefined

// A worker session: ATHENA_WORKER set, in its environment or its settings.
async function readWorker($: EngineInterface): Promise<boolean> {
  return Boolean((await $.env.get('ATHENA_WORKER')) ?? (await settingsEnv($)).ATHENA_WORKER)
}

function isWorker($: EngineInterface): Promise<boolean> {
  worker ??= readWorker($).catch(() => false)
  return worker
}

// Read lazily and once: a render on --resume can come before session.start settles.
function isHq($: EngineInterface): Promise<boolean> {
  role ??= readRole($).catch(() => false)
  return role
}

let linking: Promise<boolean> | undefined

// Whether a Link draws as a masked OSC 8 span here, read once from the variables Claude Code decides it by.
async function readLinking($: EngineInterface): Promise<boolean> {
  // Each name a literal, so validate lists what the mod reads.
  const read: Record<(typeof LINK_ENV)[number], string | undefined> = {
    FORCE_HYPERLINK: await $.env.get('FORCE_HYPERLINK'),
    TERM_PROGRAM: await $.env.get('TERM_PROGRAM'),
    TERM_PROGRAM_VERSION: await $.env.get('TERM_PROGRAM_VERSION'),
    LC_TERMINAL: await $.env.get('LC_TERMINAL'),
    TERM: await $.env.get('TERM'),
    TERMINAL_EMULATOR: await $.env.get('TERMINAL_EMULATOR'),
    WT_SESSION: await $.env.get('WT_SESSION'),
    TMUX: await $.env.get('TMUX'),
    VTE_VERSION: await $.env.get('VTE_VERSION'),
  }
  // A settings file's env (HQ's sets FORCE_HYPERLINK) counts as the engine's own.
  const settings = await settingsEnv($)
  const env: Record<string, string | undefined> = {}
  for (const key of LINK_ENV) {
    const value = read[key] ?? settings[key]
    env[key] = typeof value === 'string' ? value : undefined
  }
  return hyperlinks(env)
}

function isLinking($: EngineInterface): Promise<boolean> {
  linking ??= readLinking($).catch(() => false)
  return linking
}

let workerRepo: Promise<string | undefined> | undefined

// A worker's own repo, `owner/repo` from its worktree's origin remote: what its report's PR links into.
function readWorkerRepo($: EngineInterface): Promise<string | undefined> {
  workerRepo ??= $.process
    .run(['git', 'remote', 'get-url', 'origin'])
    .then(r => (r.exitCode === 0 ? repoSlug(r.stdout) : undefined))
    .catch(() => undefined)
  return workerRepo
}

type Elements = ReturnType<EngineInterface['ui']['resolve']>

/**
 * Drawn parts: each one with an `href` a masked link where the terminal draws one, plain text otherwise (never the
 * engine's `label URL` fallback). Display only: the text a wake submits never carries a link.
 */
function drawParts(ui: Elements, parts: readonly Part[], isOn: boolean, style: { color?: string; dimColor?: boolean } = {}) {
  const { Text, Link } = ui
  return parts.map(part =>
    part.href && isOn ? (
      <Link href={part.href}>
        <Text {...style}>{part.text}</Text>
      </Link>
    ) : (
      <Text {...style}>{part.text}</Text>
    ),
  )
}

function show(next: { board?: Board; note?: string }, $: EngineInterface): void {
  board = next.board ?? board
  note = next.note
  $.ui.invalidate('ui.render')
}

/** Minutes east of UTC: the module's own clock reads UTC, so ask the system once. */
async function utcOffset($: EngineInterface): Promise<number> {
  try {
    const m = /^([+-])(\d\d)(\d\d)$/.exec((await $.process.run(['date', '+%z'])).stdout.trim())
    if (m) return (m[1] === '-' ? -1 : 1) * (Number(m[2]) * 60 + Number(m[3]))
  } catch {
    // fall through
  }
  return -new Date().getTimezoneOffset()
}

async function told($: EngineInterface, key: string): Promise<Record<string, number>> {
  const value = await $.store.get(key).catch(() => undefined)
  if (!value || typeof value !== 'object') return {}
  return Object.fromEntries(Object.entries(value).filter((kv): kv is [string, number] => typeof kv[1] === 'number'))
}

async function watch($: EngineInterface): Promise<void> {
  $.ui.status(undefined) // the band replaces the engine's status line
  const offset = await utcOffset($)
  const key = await toldKey($)
  const w = (waker = new Waker({
    // asUser: stored bare, so the row shows the `athena watch HH:MM:` prompt without the engine's plugin frame and
    // its "This is how Claude Code surfaces a prompt..." sentence. The header names the mod; hq.md and the skill tell the
    // model it is the mod's notice, not the human.
    submit: text => {
      lastWake = parseWake(text)
      $.ui.invalidate('ui.render')
      return $.prompt.submit({ text, asUser: true })
    },
    after: (ms, fn) => $.clock.after(ms, fn),
    now: () => Date.now(),
    stamp: () => clockText(Date.now(), offset),
    sent: await told($, key),
    remember: sent => void $.store.set(key, sent).catch(() => undefined),
  }))
  const command = (await $.env.get('ATHENA_WATCH_CMD')) || `${$.plugin.root}/bin/athena`
  let quickExits = 0
  for (;;) {
    show({ note: 'watch starting' }, $)
    const startedAt = Date.now()
    let rest = ''
    let errors = ''
    let how = ''
    try {
      const child = $.process.spawn({ argv: [command, 'watch', '--tagged'] })
      for (;;) {
        const step = await child.next()
        if (step.done) {
          how = step.value.signal ? `signal ${step.value.signal}` : `code ${step.value.code}`
          break
        }
        const { stream, text } = step.value
        if (stream === 'stderr') {
          errors = (errors + text).slice(-2000)
          continue
        }
        const read = readRecords(rest, text)
        rest = read.rest
        for (const r of read.records) {
          if ('status' in r) show({ board: readBoard(r.board), note: readBoard(r.board) ? undefined : r.status }, $)
          else if (r.wake) w.add(r.line)
          else w.settle(r.line)
        }
      }
    } catch (err) {
      how = `could not start ${command}: ${err instanceof Error ? err.message : String(err)}`
    }
    quickExits = Date.now() - startedAt < STEADY_MS ? quickExits + 1 : 0
    const delay = restartDelay(Math.max(quickExits, 1))
    const lastError = errors.trim().split('\n').pop()
    // Told once per run of quick exits, so a watch that cannot start does not wake HQ every minute.
    if (quickExits <= 1) w.add(`watch error: exited (${how})${lastError ? `: ${lastError}` : ''}; restarting`)
    show({ note: `watch exited (${how}); restarting in ${Math.round(delay / 1000)}s` }, $)
    await new Promise<void>(resolve => $.clock.after(delay, resolve))
  }
}

async function hidden($: EngineInterface, e: Parameters<EngineInterface['ui']['resolve']>[0]) {
  const { Box } = $.ui.resolve(e)
  return <Box display="none" />
}

const TONE: Record<Segment['tone'], string | undefined> = { needs: 'warning', active: undefined, done: 'success', dash: undefined }

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    if (!isWatching && (await isHq($))) {
      isWatching = true
      void watch($)
    }
    return started
  })

  // The person typed: they have seen the last wake, so its row leaves the band.
  on('prompt.submit', async ($, e, next) => {
    if (e.origin.kind === 'composer' && lastWake) {
      lastWake = undefined
      $.ui.invalidate('ui.render')
    }
    return next(e)
  }).catch(($, e, next) => next(e)) // never in the way of a prompt

  on('turn.start', async ($, e, next) => {
    waker?.turnStarted()
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) waker?.turnEnded()
    return next(e)
  })

  // A wake drawn as athena's own compact row: its mark, a dim time, one event per line (every event, so ctrl+o shows
  // the same; isExpanded is also true under a speaker label, which this row has). Claude Code 2.1.296 raises no ui.render for a plugin's prompt row (its own `› Prompt from the
  // athena plugin` block), so this waits for a build that does; until then `asUser` keeps the frame off the row.
  on('ui.render', { component: 'UserMessage', props: { origin: { kind: 'plugin', name: 'athena' } } }, async ($, e, next) => {
    const wake = parseWake(e.props.text)
    if (!wake || !(await isHq($))) return next(e)
    const ui = $.ui.resolve(e)
    const { Box, Text } = ui
    const isOn = await isLinking($)
    return (
      <Box flexDirection="row" columnGap={1}>
        <Text color={MADDER} bold>
          {`${MARK} athena`}
        </Text>
        {wake.time ? <Text dimColor>{wake.time}</Text> : null}
        <Box flexDirection="column">
          {wake.events.map(event => (
            <Text>{drawParts(ui, eventParts(event, board), isOn)}</Text>
          ))}
        </Box>
      </Box>
    )
  })

  // The band above the prompt. On top, the last wake as athena's own row: its mark, a dim time, one event per line
  // (the transcript's row of it is the engine's to draw). Then the status: what needs the human (warning), the
  // active workers, and the done ones (success), folded to fit one line. Another plugin's band still draws under it.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || !(board || note || lastWake) || !(await isHq($))) return next(e)
    const ui = $.ui.resolve(e)
    const { Box, Text } = ui
    const isOn = await isLinking($)
    const head = `${MARK} athena`
    const indent = ' '.repeat(head.length + 1)
    const segments = board && !note ? fitStatus(board, e.props.bodyColumns - head.length - 1) : []
    const status = note ? (
      <Text dimColor>{note}</Text>
    ) : (
      segments.map((s, i) => (
        <Text>
          {i ? <Text dimColor>{' · '}</Text> : ''}
          {s.tone === 'dash'
            ? drawParts(ui, [{ text: s.text, href: s.href }], isOn, { dimColor: true })
            : drawParts(ui, linkParts(s.text, boardHref(board)), isOn, { color: TONE[s.tone] })}
        </Text>
      ))
    )
    const theirs = await next(e)
    return (
      <Box flexDirection="column">
        {lastWake ? (
          <Box flexDirection="row" columnGap={1}>
            <Box flexShrink={0}>
              <Text color={MADDER} bold>
                {head}
              </Text>
            </Box>
            {lastWake.time ? (
              <Box flexShrink={0}>
                <Text dimColor>{lastWake.time}</Text>
              </Box>
            ) : null}
            <Box flexDirection="column" flexShrink={1}>
              {lastWake.events.map(event => (
                <Text wrap="truncate-end">{drawParts(ui, eventParts(event, board), isOn)}</Text>
              ))}
            </Box>
          </Box>
        ) : null}
        {board || note ? (
          <Box flexDirection="row">
            <Text wrap="truncate-end">
              {lastWake ? (
                <Text>{indent}</Text>
              ) : (
                <Text color={MADDER} bold>
                  {head}
                </Text>
              )}
              {lastWake ? '' : ' '}
              {status}
            </Text>
          </Box>
        ) : null}
        {theirs}
      </Box>
    )
  })

  // In a worker, the ATHENA-REPORT line its final message ends with draws as a completion block: the status in
  // its colour, the PR and head, the verify line, the decisions and the concerns. Drawing only: the stored message,
  // what the hooks parse and what athena reads are unchanged. A line that does not parse keeps the engine's drawing.
  on('ui.render', { component: 'AssistantMessage' }, async ($, e, next) => {
    if (e.props.isSummary || !e.props.text.includes('ATHENA-REPORT')) return next(e)
    const split = splitReport(e.props.text)
    if (!split || !(await isWorker($))) return next(e)
    const v = reportView(split.report, await readWorkerRepo($))
    const ui = $.ui.resolve(e)
    const { Box, Text } = ui
    const isOn = await isLinking($)
    const rest = [split.before, split.after].filter(Boolean).join('\n\n')
    const drawn = rest ? await next({ ...e, props: { ...e.props, text: rest } }) : null
    const row = (label: string, value: ReturnType<typeof Text>) => (
      <Box flexDirection="row">
        <Box width={10} flexShrink={0}>
          <Text dimColor>{label}</Text>
        </Box>
        <Box flexDirection="column" flexShrink={1}>
          {value}
        </Box>
      </Box>
    )
    const bullets = (items: string[], color?: string) => (
      <Box flexDirection="column">
        {items.map(item => (
          <Text color={color}>{`• ${item}`}</Text>
        ))}
      </Box>
    )
    return (
      <Box flexDirection="column">
        {drawn}
        <Box flexDirection="column" borderStyle="round" borderColor={MADDER} paddingX={1} marginTop={rest ? 1 : 0}>
          <Box flexDirection="row" columnGap={1}>
            <Text color={MADDER} bold>
              {`${MARK} athena report`}
            </Text>
            <Text color={v.status.color} bold>
              {v.status.text}
            </Text>
          </Box>
          {v.pr ? row('PR', <Text>{drawParts(ui, [{ text: v.pr.label, href: v.pr.href }], isOn)}</Text>) : null}
          {v.head ? row('head', <Text dimColor>{v.head}</Text>) : null}
          {v.verify ? row('verify', <Text>{v.verify}</Text>) : null}
          {v.decisions.length ? row('decisions', bullets(v.decisions)) : null}
          {v.concerns.length ? row('concerns', bullets(v.concerns, 'warning')) : null}
        </Box>
      </Box>
    )
  })

  // Monitor rows from before the mod (or from the manual fallback) are drawn as nothing in HQ.
  on('ui.render', { component: 'ToolUse', props: { tool: 'Monitor' } }, async ($, e, next) =>
    (await isHq($)) ? hidden($, e) : next(e),
  )

  on('ui.render', { component: 'ToolResult', props: { tool: 'Monitor' } }, async ($, e, next) =>
    (await isHq($)) ? hidden($, e) : next(e),
  )

  // A Monitor's events, end and expiry notices are task-notification rows. Drawing changes the row alone, never
  // what the model read; under ctrl+o (isExpanded) they draw in full.
  on('ui.render', { component: 'UserMessage', props: { origin: { kind: 'task-notification' } } }, async ($, e, next) =>
    // The task carries no tool name (2.1.296: id, status, type, toolUseId; a Monitor's event rows only an id), so
    // a Monitor's rows are told by the engine's own text for them.
    !e.props.isExpanded && e.props.task && isMonitorNotice(e.props.text) && (await isHq($)) ? hidden($, e) : next(e),
  )
}
