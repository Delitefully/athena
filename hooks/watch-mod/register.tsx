// athena-watch: in the HQ session alone (ATHENA_ROLE=hq), runs `athena watch --tagged` for the session's life,
// draws a one-line status band above the prompt, and wakes HQ with one short prompt only for lines that need it,
// each event once in ten minutes. A wake draws as athena's own compact row, without the engine's plugin frame.
// It also hides any Monitor rows left in HQ's transcript (the call, its result and its notifications). Every other session sees no change.
import type { EngineInterface, Register } from 'claude-code'

import type { Board, Segment } from './core'
import { clockText, fitStatus, isMonitorNotice, parseWake, readBoard, readRecords, restartDelay, Waker } from './core'

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

// HQ only: ATHENA_ROLE=hq, and never a worker, even one that inherited HQ's environment.
async function readRole($: EngineInterface): Promise<boolean> {
  const env = (await $.settings.read()).env as Record<string, unknown> | undefined
  const role = (await $.env.get('ATHENA_ROLE')) ?? env?.ATHENA_ROLE
  const worker = (await $.env.get('ATHENA_WORKER')) ?? env?.ATHENA_WORKER
  return role === 'hq' && !worker
}

// Read lazily and once: a render on --resume can come before session.start settles.
function isHq($: EngineInterface): Promise<boolean> {
  role ??= readRole($).catch(() => false)
  return role
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
    // its "This is how Claude Code surfaces a prompt..." sentence. The header names the mod; hq.md tells the model.
    submit: text => $.prompt.submit({ text, asUser: true }),
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

const TONE: Record<Segment['tone'], string | undefined> = { needs: 'warning', active: undefined, done: 'success' }

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    if (!isWatching && (await isHq($))) {
      isWatching = true
      void watch($)
    }
    return started
  })

  on('turn.start', async ($, e, next) => {
    waker?.turnStarted()
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) waker?.turnEnded()
    return next(e)
  })

  // A wake drawn as athena's own compact row: its mark, a dim time, one event per line; ctrl+o (isExpanded) shows
  // the engine's row. Claude Code 2.1.296 raises no ui.render for a plugin's prompt row (its own `› Prompt from the
  // athena plugin` block), so this waits for a build that does; until then `asUser` keeps the frame off the row.
  on('ui.render', { component: 'UserMessage', props: { origin: { kind: 'plugin', name: 'athena' } } }, async ($, e, next) => {
    const wake = !e.props.isExpanded && parseWake(e.props.text)
    if (!wake || !(await isHq($))) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box flexDirection="row" columnGap={1}>
        <Text color={MADDER} bold>
          {`${MARK} athena`}
        </Text>
        {wake.time ? <Text dimColor>{wake.time}</Text> : null}
        <Box flexDirection="column">
          {wake.events.map(event => (
            <Text>{event}</Text>
          ))}
        </Box>
      </Box>
    )
  })

  // The status band above the prompt: athena's mark, then what needs the human (warning), the active workers, and
  // the done ones (success), folded to fit one line. Another plugin's band still draws under it.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || !(board || note) || !(await isHq($))) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    const head = `${MARK} athena`
    const segments = board && !note ? fitStatus(board, e.props.bodyColumns - head.length - 1) : []
    const theirs = await next(e)
    return (
      <Box flexDirection="column">
        <Box flexDirection="row">
          <Text wrap="truncate-end">
            <Text color={MADDER} bold>
              {head}
            </Text>
            {' '}
            {note ? (
              <Text dimColor>{note}</Text>
            ) : (
              segments.map((s, i) => (
                <Text>
                  {i ? <Text dimColor>{' · '}</Text> : ''}
                  <Text color={TONE[s.tone]}>{s.text}</Text>
                </Text>
              ))
            )}
          </Text>
        </Box>
        {theirs}
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
