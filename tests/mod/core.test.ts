import { describe, expect, test } from 'claude-code/testing'

import { MAX_LINES, promptText, readRecords, restartDelay, Waker } from '../../hooks/watch-mod/core.ts'

/** A hand-moved clock for the Waker: timers fire only when the test advances it. */
function harness(opts: { batchMs?: number; submitResolves?: boolean } = {}) {
  let now = 0
  let timers: { at: number; fn: () => void; isCancelled: boolean }[] = []
  const sent: string[] = []
  const pendingSubmits: (() => void)[] = []
  const w = new Waker({
    batchMs: opts.batchMs ?? 3000,
    now: () => now,
    after: (ms, fn) => {
      const t = { at: now + ms, fn, isCancelled: false }
      timers.push(t)
      return { cancel: () => (t.isCancelled = true) }
    },
    submit: text => {
      sent.push(text)
      if (opts.submitResolves === false) return new Promise<void>(resolve => pendingSubmits.push(resolve))
      return Promise.resolve()
    },
  })
  const advance = async (ms: number) => {
    now += ms
    for (;;) {
      const due = timers.filter(t => !t.isCancelled && t.at <= now).sort((a, b) => a.at - b.at)[0]
      if (!due) break
      timers = timers.filter(t => t !== due)
      due.fn()
      await Promise.resolve()
      await Promise.resolve()
    }
    for (let i = 0; i < 5; i++) await Promise.resolve()
  }
  return { w, sent, advance, pendingSubmits }
}

describe('readRecords', () => {
  test('joins pieces into lines and parses tagged records', () => {
    const a = readRecords('', '{"line":"a report DONE","wake":true}\n{"status":"a do')
    expect(a.records).toEqual([{ line: 'a report DONE', wake: true }])
    const b = readRecords(a.rest, 'ne"}\n{"line":"a state working->idle","wake":false}\n')
    expect(b.records).toEqual([{ status: 'a done' }, { line: 'a state working->idle', wake: false }])
    expect(b.rest).toBe('')
  })

  test('an untagged line is actionable as is', () => {
    expect(readRecords('', 'Traceback (most recent call last):\n\n').records).toEqual([
      { line: 'Traceback (most recent call last):', wake: true },
    ])
  })
})

describe('promptText', () => {
  test('one terse line', () => {
    expect(promptText(['plt-4041 report DONE'])).toBe('athena watch: plt-4041 report DONE')
    expect(promptText(['plt-4041 report DONE', 'pr6519 checks pending->failure'])).toBe(
      'athena watch: plt-4041 report DONE · pr6519 checks pending->failure',
    )
  })

  test('caps a burst', () => {
    const lines = Array.from({ length: MAX_LINES + 3 }, (_, i) => `w${i} report DONE`)
    expect(promptText(lines).endsWith(' · +3 more (athena status)')).toBe(true)
  })
})

describe('Waker', () => {
  test('lines within the window go out as one prompt', async () => {
    const { w, sent, advance } = harness()
    w.add('plt-4041 report DONE')
    await advance(1000)
    w.add('pr6519 checks pending->failure')
    expect(sent).toEqual([])
    await advance(2000)
    expect(sent).toEqual(['athena watch: plt-4041 report DONE · pr6519 checks pending->failure'])
    await advance(10000)
    expect(sent.length).toBe(1)
  })

  test('waits while HQ runs a turn, then sends everything once', async () => {
    const { w, sent, advance } = harness()
    w.turnStarted()
    w.add('a report DONE')
    await advance(5000)
    w.add('b review APPROVED')
    await advance(60000)
    expect(sent).toEqual([])
    w.turnEnded()
    await advance(3000)
    expect(sent).toEqual(['athena watch: a report DONE · b review APPROVED'])
  })

  test('holds new lines until the previous prompt has started its turn', async () => {
    const { w, sent, advance, pendingSubmits } = harness({ submitResolves: false })
    w.add('a report DONE')
    await advance(3000)
    w.add('b report BLOCKED')
    await advance(10000)
    expect(sent.length).toBe(1)
    pendingSubmits[0]!()
    await advance(0)
    await advance(3000)
    expect(sent).toEqual(['athena watch: a report DONE', 'athena watch: b report BLOCKED'])
  })

  test('the same watch error is told once in ten minutes', async () => {
    const { w, sent, advance } = harness()
    w.add('watch error: gh: rate limited')
    await advance(3000)
    w.add('watch error: gh: rate limited')
    await advance(3000)
    expect(sent.length).toBe(1)
    await advance(10 * 60 * 1000)
    w.add('watch error: gh: rate limited')
    await advance(3000)
    expect(sent.length).toBe(2)
  })

  test('a duplicate line in one batch is told once', async () => {
    const { w, sent, advance } = harness()
    w.add('a report DONE')
    w.add('a report DONE')
    await advance(3000)
    expect(sent).toEqual(['athena watch: a report DONE'])
  })
})

test('restart delay backs off to a minute', () => {
  expect([1, 2, 3, 4, 5, 9].map(restartDelay)).toEqual([5000, 10000, 20000, 40000, 60000, 60000])
})
