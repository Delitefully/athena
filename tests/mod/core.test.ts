import { describe, expect, test } from 'claude-code/testing'

import {
  fitStatus,
  isMonitorNotice,
  MAX_LINES,
  ORANGE,
  parseWake,
  promptText,
  readRecords,
  reportView,
  restartDelay,
  splitReport,
  Waker,
} from '../../hooks/watch-mod/core'
import type { Board } from '../../hooks/watch-mod/core'

/** The prompt for a batch stamped 22:19, one event per line. */
const p = (...lines: string[]) => `athena watch 22:19:\n${lines.join('\n')}`

/** A hand-moved clock for the Waker: timers fire only when the test advances it. */
function harness(opts: { batchMs?: number; submitResolves?: boolean | 'reject-once'; sent?: Record<string, number> } = {}) {
  let now = 0
  let timers: { at: number; fn: () => void; isCancelled: boolean }[] = []
  const sent: string[] = []
  const pendingSubmits: (() => void)[] = []
  const remembered: Record<string, number>[] = []
  const w = new Waker({
    batchMs: opts.batchMs ?? 3000,
    now: () => now,
    stamp: () => '22:19',
    sent: opts.sent,
    remember: told => remembered.push(told),
    after: (ms, fn) => {
      const t = { at: now + ms, fn, isCancelled: false }
      timers.push(t)
      return { cancel: () => (t.isCancelled = true) }
    },
    submit: text => {
      sent.push(text)
      if (opts.submitResolves === false) return new Promise<void>(resolve => pendingSubmits.push(resolve))
      if (opts.submitResolves === 'reject-once' && sent.length === 1) return Promise.reject(new Error('refused'))
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
  return { w, sent, advance, pendingSubmits, remembered, at: () => now }
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
  test('a stamped header, then one event per line', () => {
    expect(promptText(['plt-4041 report DONE'], '22:19')).toBe(p('plt-4041 report DONE'))
    expect(promptText(['plt-4041 report DONE', 'pr6519 checks pending->failure'], '22:19')).toBe(
      'athena watch 22:19:\nplt-4041 report DONE\npr6519 checks pending->failure',
    )
    expect(promptText(['a report DONE'])).toBe('athena watch:\na report DONE')
  })

  test('caps a burst', () => {
    const lines = Array.from({ length: MAX_LINES + 3 }, (_, i) => `w${i} report DONE`)
    expect(promptText(lines, '22:19').endsWith('\n+3 more (athena status)')).toBe(true)
  })
})

describe('parseWake', () => {
  test('reads back the stamp and the events', () => {
    expect(parseWake(p('a state working->blocked', 'a report BLOCKED'))).toEqual({
      time: '22:19',
      events: ['a state working->blocked', 'a report BLOCKED'],
    })
  })

  test('drops the engine frame around a plugin prompt', () => {
    const framed =
      'The athena plugin sent a message:\n' +
      p('a report DONE') +
      "\n\nThis is how Claude Code surfaces a prompt a plugin submits between turns — it starts this turn in the user's place. Address the message above."
    expect(parseWake(framed)).toEqual({ time: '22:19', events: ['a report DONE'] })
  })

  test('reads the one-line prompts sent before the stamp', () => {
    expect(parseWake('athena watch: a report DONE · b checks pending->failure')).toEqual({
      time: undefined,
      events: ['a report DONE', 'b checks pending->failure'],
    })
  })

  test('anything else is not a wake', () => {
    expect(parseWake('athena: what is plt-4041 doing?')).toBeUndefined()
    expect(parseWake('athena watch 22:19:')).toBeUndefined()
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
    expect(sent).toEqual([p('plt-4041 report DONE', 'pr6519 checks pending->failure')])
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
    expect(sent).toEqual([p('a report DONE', 'b review APPROVED')])
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
    expect(sent).toEqual([p('a report DONE'), p('b report BLOCKED')])
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

  test('the same event within ten minutes wakes HQ once', async () => {
    // pr6519-design was re-prompted, ran, and ended BLOCKED again: athena watch said the same two lines 3.5 min apart.
    const { w, sent, advance } = harness()
    w.add('pr6519-design state working->blocked')
    w.add('pr6519-design report BLOCKED')
    await advance(3000)
    await advance(3.5 * 60 * 1000)
    w.add('pr6519-design state working->blocked')
    w.add('pr6519-design report BLOCKED')
    await advance(3000)
    expect(sent).toEqual([p('pr6519-design state working->blocked', 'pr6519-design report BLOCKED')])
    await advance(10 * 60 * 1000)
    w.add('pr6519-design report BLOCKED')
    await advance(3000)
    expect(sent.length).toBe(2)
  })

  test('a repeat is dropped alone: a new event in its batch still goes out', async () => {
    const { w, sent, advance } = harness()
    w.add('a report BLOCKED')
    await advance(3000)
    w.add('a report BLOCKED')
    w.add('b report DONE')
    await advance(3000)
    expect(sent).toEqual([p('a report BLOCKED'), p('b report DONE')])
  })

  test('what was told is remembered, so a reload or HQ restart does not tell it again', async () => {
    const first = harness()
    first.w.add('a report BLOCKED')
    await first.advance(3000)
    const told = first.remembered.at(-1)!
    expect(told).toEqual({ 'a report BLOCKED': 3000 })
    const second = harness({ sent: told })
    await second.advance(60 * 1000)
    second.w.add('a report BLOCKED')
    await second.advance(3000)
    expect(second.sent).toEqual([])
  })

  test('old entries are pruned from what is remembered', async () => {
    const { w, advance, remembered } = harness({ sent: { 'old report DONE': -11 * 60 * 1000 } })
    w.add('a report DONE')
    await advance(3000)
    expect(remembered.at(-1)).toEqual({ 'a report DONE': 3000 })
  })

  test('a refused submit is tried again, not dropped as a repeat', async () => {
    const { w, sent, advance } = harness({ submitResolves: 'reject-once' })
    w.add('a report DONE')
    await advance(3000)
    await advance(3000)
    expect(sent).toEqual([p('a report DONE'), p('a report DONE')])
  })

  test('a duplicate line in one batch is told once', async () => {
    const { w, sent, advance } = harness()
    w.add('a report DONE')
    w.add('a report DONE')
    await advance(3000)
    expect(sent).toEqual([p('a report DONE')])
  })
})

describe('fitStatus', () => {
  const board: Board = {
    needs: ['pr6519-design blocked'],
    prs: ['#5856', '#6547', '#6552'],
    active: [['working', ['wake-ui', 'plt-7']]],
    done: ['plt-4041', 'plt-4042'],
  }
  const texts = (columns: number) => fitStatus(board, columns).map(s => `${s.tone}:${s.text}`)

  test('needs you first, then the active workers, then the done ones, in full when they fit', () => {
    expect(texts(200)).toEqual([
      'needs:pr6519-design blocked',
      'needs:#5856 #6547 #6552 need you',
      'active:wake-ui, plt-7 working',
      'done:✓ plt-4041, plt-4042 done',
    ])
  })

  test('narrower, the done ones fold to a count first, then the active ones, then the PRs, then needs you', () => {
    expect(texts(90)).toEqual([
      'needs:pr6519-design blocked',
      'needs:#5856 #6547 #6552 need you',
      'active:wake-ui, plt-7 working',
      'done:✓ 2 done',
    ])
    expect(texts(75)).toEqual(['needs:pr6519-design blocked', 'needs:#5856 #6547 #6552 need you', 'active:2 working', 'done:✓ 2 done'])
    expect(texts(65)).toEqual(['needs:pr6519-design blocked', 'needs:3 PRs need you', 'active:2 working', 'done:✓ 2 done'])
    expect(texts(20)).toEqual(['needs:1 blocked', 'needs:3 PRs need you', 'active:2 working', 'done:✓ 2 done'])
  })

  test('one PR, several kinds of need, and nothing at all', () => {
    const one: Board = { needs: ['a blocked', 'b needs context', 'c blocked'], prs: ['#7'], active: [], done: [] }
    expect(fitStatus(one, 200).map(s => s.text)).toEqual(['a blocked', 'b needs context', 'c blocked', '#7 needs you'])
    expect(fitStatus(one, 10).map(s => s.text)).toEqual(['2 blocked', '1 needs context', '1 PR needs you'])
    expect(fitStatus({ needs: [], prs: [], active: [], done: [] }, 80)).toEqual([{ text: 'no live workers', tone: 'active' }])
  })
})

test('restart delay backs off to a minute', () => {
  expect([1, 2, 3, 4, 5, 9].map(restartDelay)).toEqual([5000, 10000, 20000, 40000, 60000, 60000])
})

test('Monitor notification rows are told apart from other task notifications', () => {
  expect(isMonitorNotice('Monitor event: "tick test (echo tick; sleep 1)"')).toBe(true)
  expect(isMonitorNotice('Monitor "tick test (echo tick; sleep 1)" stream ended')).toBe(true)
  expect(isMonitorNotice('[Monitor expired after 30m: athena watch]')).toBe(true)
  expect(isMonitorNotice('Background command "make test" completed (exit code 0)')).toBe(false)
  expect(isMonitorNotice('Agent "reviewer" completed')).toBe(false)
})

describe('splitReport', () => {
  const json = (o: object) => JSON.stringify(o)
  const done = { status: 'DONE', pr: 'https://github.com/o/r/pull/6553', head: '797b67975c13', verify: 'make test -> pass', decisions: ['a', 'b'], concerns: [] }

  test('finds the line, parses it, and keeps the text around it', () => {
    const text = `Pushed and opened the PR.\n\nATHENA-REPORT ${json(done)}`
    expect(splitReport(text)).toEqual({ before: 'Pushed and opened the PR.', after: '', report: { ...done, concerns: [] } })
  })

  test('a line in backticks or a code fence, and text after it', () => {
    expect(splitReport(`x\n\`ATHENA-REPORT ${json(done)}\``)?.before).toBe('x')
    const fenced = splitReport(`x\n\`\`\`\nATHENA-REPORT ${json(done)}\n\`\`\`\nthanks`)
    expect([fenced?.before, fenced?.after, fenced?.report.status]).toEqual(['x', 'thanks', 'DONE'])
  })

  test('malformed or missing: no report, so the engine draws the message as is', () => {
    expect(splitReport('ATHENA-REPORT {"status":"DONE",')).toBeUndefined()
    expect(splitReport('ATHENA-REPORT {"pr":"x"}')).toBeUndefined()
    expect(splitReport('ATHENA-REPORT ["DONE"]')).toBeUndefined()
    expect(splitReport('no report here')).toBeUndefined()
    expect(splitReport('see the ATHENA-REPORT {"status":"DONE"} format')).toBeUndefined()
  })
})

describe('reportView', () => {
  const base = { pr: 'https://github.com/o/r/pull/6553', head: '797b67975c13', verify: 'make test -> pass', decisions: ['d1'], concerns: ['c1'] }

  test('each status has its word and colour', () => {
    const look = (status: string) => reportView({ ...base, status }).status
    expect(look('DONE')).toEqual({ text: 'Done', color: 'success' })
    expect(look('DONE_WITH_CONCERNS')).toEqual({ text: 'Done with concerns', color: 'warning' })
    expect(look('BLOCKED')).toEqual({ text: 'Blocked', color: 'error' })
    expect(look('NEEDS_CONTEXT')).toEqual({ text: 'Needs context', color: ORANGE })
    expect(look('SOMETHING_ELSE')).toEqual({ text: 'SOMETHING_ELSE', color: undefined })
  })

  test('the PR as #n with its head, the verify line, and the lists', () => {
    expect(reportView({ ...base, status: 'DONE' })).toEqual({
      status: { text: 'Done', color: 'success' },
      pr: { href: 'https://github.com/o/r/pull/6553', label: '#6553', head: '797b679' },
      verify: 'make test -> pass',
      decisions: ['d1'],
      concerns: ['c1'],
    })
  })

  test('what is empty is left out', () => {
    expect(reportView({ status: 'BLOCKED', pr: '', head: '', verify: '', decisions: [], concerns: [] })).toEqual({
      status: { text: 'Blocked', color: 'error' },
      pr: undefined,
      verify: undefined,
      decisions: [],
      concerns: [],
    })
    expect(reportView({ status: 'DONE', pr: 'not a url', decisions: [], concerns: [] }).pr).toEqual({
      href: undefined,
      label: 'not a url',
      head: undefined,
    })
  })
})
