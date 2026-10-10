// athena-watch: in the HQ session alone (ATHENA_ROLE=hq), runs `athena watch --tagged` for the session's life,
// pins a one-line summary under the prompt, and wakes HQ with one short prompt only for lines that need it.
// It also hides any Monitor rows left in HQ's transcript. Every other session sees no change.
import type { EngineInterface, Register } from 'claude-code'

import { readRecords, restartDelay, Waker } from './core.ts'

/** A run shorter than this counts as a quick exit, and the next start waits longer. */
const STEADY_MS = 60000

let role: Promise<boolean> | undefined
let isWatching = false
let waker: Waker | undefined

async function readRole($: EngineInterface): Promise<boolean> {
  const fromEnv = await $.env.get('ATHENA_ROLE')
  if (fromEnv !== undefined) return fromEnv === 'hq'
  const env = (await $.settings.read()).env as Record<string, unknown> | undefined
  return env?.ATHENA_ROLE === 'hq'
}

// Read lazily and once: a render on --resume can come before session.start settles.
function isHq($: EngineInterface): Promise<boolean> {
  role ??= readRole($).catch(() => false)
  return role
}

async function watch($: EngineInterface): Promise<void> {
  const w = (waker = new Waker({
    submit: text => $.prompt.submit({ text }),
    after: (ms, fn) => $.clock.after(ms, fn),
    now: () => Date.now(),
  }))
  const command = (await $.env.get('ATHENA_WATCH_CMD')) || `${$.plugin.root}/bin/athena`
  let quickExits = 0
  for (;;) {
    $.ui.status('athena · watch starting')
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
          if ('status' in r) $.ui.status(r.status)
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
    if (quickExits <= 1) w.add(`watch error: athena watch exited (${how})${lastError ? `: ${lastError}` : ''}; restarting`)
    $.ui.status(`athena · watch exited (${how}); restarting in ${Math.round(delay / 1000)}s`)
    await new Promise<void>(resolve => $.clock.after(delay, resolve))
  }
}

async function hidden($: EngineInterface, e: Parameters<EngineInterface['ui']['resolve']>[0]) {
  const { Box } = $.ui.resolve(e)
  return <Box display="none" />
}

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

  // Monitor rows from before the mod (or from the manual fallback) are drawn as nothing in HQ.
  on('ui.render', { component: 'ToolUse', props: { tool: 'Monitor' } }, async ($, e, next) =>
    (await isHq($)) ? hidden($, e) : next(e),
  )

  on('ui.render', { component: 'ToolResult', props: { tool: 'Monitor' } }, async ($, e, next) =>
    (await isHq($)) ? hidden($, e) : next(e),
  )
}
