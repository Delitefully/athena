// athena-rex's progress band, in a worker's session only (ATHENA_WORKER set; athena-rex launches workers alone with
// this plugin). One line above the prompt: `▰▰▱▱▱ 1/3 · writing the hook · 4m · task list`, read from
// workers/<name>.progress.json, which athena-rex's PostToolUse hook writes from the worker's own task list. Drawing
// costs no model call. It repaints when that file or the worker's status file changes, looked at on the session's own
// events (a tool call, a turn's start and end), never on a timer of its own. When the task list has been quiet for
// ten minutes while the worker works, it asks `athena-rex progress ask` whether a Haiku estimate is due, and if so
// sends that capped request through the session's own client and hands the reply to `athena-rex progress answer`.
import type { EngineInterface, Register } from 'claude-code'

import type { Band, Progress } from './core'
import { band, parseObject, parseProgress, QUIET_MS, transcriptLines } from './core'

/** athena's colour (docs/brand.md of athena): lit madder. */
const MADDER = '#E0573D'
const NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/

type Worker = { name: string; dir: string; bin: string }

let worker: Promise<Worker | undefined> | undefined
let progress: Progress | undefined
let state: string | undefined
let seen = ''
let shown: Band | undefined
let inTurn = false
let startedAt = Date.now()
let askedAt = 0
let asking = false

async function settingsEnv($: EngineInterface): Promise<Record<string, unknown>> {
  const env = await $.settings.read().then(s => s.env as Record<string, unknown> | undefined, () => undefined)
  return env ?? {}
}

// A worker: ATHENA_WORKER set in its environment or its settings, and never HQ.
async function readWorker($: EngineInterface): Promise<Worker | undefined> {
  const env = await settingsEnv($)
  const name = (await $.env.get('ATHENA_WORKER')) ?? env.ATHENA_WORKER
  const role = (await $.env.get('ATHENA_ROLE')) ?? env.ATHENA_ROLE
  if (typeof name !== 'string' || !NAME.test(name) || role === 'hq') return undefined
  const given = (await $.env.get('ATHENA_STATE_DIR')) ?? env.ATHENA_STATE_DIR
  const dir = typeof given === 'string' && given ? given : `${await $.env.get('HOME')}/Library/Application Support/athena`
  return { name, dir, bin: `${$.plugin.root}/../bin/athena-rex` }
}

function isWorker($: EngineInterface): Promise<Worker | undefined> {
  worker ??= readWorker($).catch(() => undefined)
  return worker
}

async function mtime($: EngineInterface, path: string): Promise<number> {
  return $.fs.stat(path).then(s => s.mtimeMs, () => 0)
}

// Re-read the two files only when one changed, and redraw only when the line would.
async function refresh($: EngineInterface): Promise<void> {
  const w = await isWorker($)
  if (!w) return
  const progressFile = `${w.dir}/workers/${w.name}.progress.json`
  const statusFile = `${w.dir}/workers/${w.name}.json`
  const key = `${await mtime($, progressFile)},${await mtime($, statusFile)}`
  if (key !== seen) {
    seen = key
    progress = parseProgress(await $.fs.read(progressFile).catch(() => undefined))
    const status = parseObject(await $.fs.read(statusFile).catch(() => undefined))?.state
    state = typeof status === 'string' ? status : undefined
  }
  const next = band(progress, state, Date.now() / 1000)
  if (JSON.stringify(next) !== JSON.stringify(shown)) {
    shown = next
    $.ui.invalidate('ui.render')
  }
}

// The Haiku estimate: only mid-turn, and no sooner than ten minutes after the session started, the last ask or the
// last task-list update. athena-rex decides for good (the worker working, progress_haiku, its own ten minutes).
async function estimate($: EngineInterface): Promise<void> {
  const w = await isWorker($)
  const now = Date.now()
  const listAge = progress?.source === 'tasks' ? now - progress.tasksTs * 1000 : Infinity
  if (!w || !inTurn || asking || now - startedAt < QUIET_MS || now - askedAt < QUIET_MS || listAge < QUIET_MS) return
  asking = true
  askedAt = now
  try {
    const env = { ATHENA_WORKER: w.name, ATHENA_STATE_DIR: w.dir }
    const lines = transcriptLines(await $.session.messages())
    const asked = await $.process.run([w.bin, 'progress', 'ask', '--name', w.name, '--since', String(startedAt / 1000)], {
      stdin: lines.join('\n'),
      env,
      timeoutMs: 15000,
    })
    const request = asked.exitCode === 0 ? parseObject(asked.stdout.trim()) : undefined
    if (!request || typeof request.prompt !== 'string' || typeof request.model !== 'string') return
    const reply = await $.model.complete({
      model: request.model,
      prompt: request.prompt,
      system: typeof request.system === 'string' ? request.system : undefined,
      maxTokens: Math.min(Number(request.maxTokens) || 40, 60),
      effort: 'low',
      timeoutMs: 30000,
    })
    if (!reply.isAnswered) return
    await $.process.run([w.bin, 'progress', 'answer', '--name', w.name], { stdin: reply.text, env, timeoutMs: 15000 })
    await refresh($)
  } finally {
    asking = false
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    startedAt = Date.now()
    void refresh($).catch(() => undefined)
    return started
  })

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) inTurn = true
    void refresh($).catch(() => undefined)
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) inTurn = false
    void refresh($).catch(() => undefined)
    return next(e)
  })

  // After any tool call: its PostToolUse hook may have written the file. Never in the way of the call.
  on('tool.call', async ($, e, next) => {
    const result = await next(e)
    void refresh($)
      .then(() => estimate($))
      .catch(() => undefined)
    return result
  }).catch(($, e, next) => next(e))

  // Drawing also looks at the files (two stats; a redraw only if the line changed), so a file written while nothing
  // else happened, such as the status hook's at session start, shows on the next draw.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    void refresh($).catch(() => undefined)
    const b = shown
    if (e.props.hasSurvey || !b) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    const theirs = await next(e)
    const line =
      b.kind === 'state' ? (
        <Text wrap="truncate-end" dimColor>
          {b.state}
        </Text>
      ) : (
        <Text wrap="truncate-end">
          <Text color={MADDER}>{b.bar}</Text>
          <Text bold>{` ${b.count}`}</Text>
          {b.step ? <Text>{` · ${b.step}`}</Text> : ''}
          <Text dimColor>{` · ${b.age} · ${b.source}`}</Text>
        </Text>
      )
    return (
      <Box flexDirection="column">
        <Box flexDirection="row">{line}</Box>
        {theirs}
      </Box>
    )
  })
}
