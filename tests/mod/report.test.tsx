// The worker's completion block, drawn through the engine on the terminal surface: one per status, and the
// engine's own drawing whenever the line does not parse.
import type { On } from 'claude-code'
import type { Engine } from 'claude-code/testing'
import { describe, expect, mock, test } from 'claude-code/testing'

/** Stands in for the engine's own drawing of the message, and for the worktree's origin remote. */
function engine(on: On, remote = 'git@github.com:o/r.git') {
  on('ui.render', { component: 'AssistantMessage' }, async ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>{e.props.text}</Text>
  })
  on('process.run', async ($, e, next) =>
    e.argv.join(' ') === 'git remote get-url origin' ? { value: { exitCode: 0, stdout: `${remote}\n`, stderr: '' } } : next(e),
  )
}

const report = (o: object) => `Pushed and opened the PR.\n\nATHENA-REPORT ${JSON.stringify(o)}`
const FULL = {
  pr: 'https://github.com/o/r/pull/6553',
  head: '797b67975c13',
  verify: 'make test -> pass',
  decisions: ['kept the frame for the model'],
  concerns: ['needs a human look'],
}

const mount = ($: Engine, text: string) =>
  $.ui.mount({ plugin: 'athena', surface: 'terminal', component: 'AssistantMessage', props: { text, isFirstOfReply: true } })

describe('a worker draws its ATHENA-REPORT as a completion block', () => {
  for (const [status, word, color] of [
    ['DONE', 'Done', 'success'],
    ['DONE_WITH_CONCERNS', 'Done with concerns', 'warning'],
    ['BLOCKED', 'Blocked', 'error'],
    ['NEEDS_CONTEXT', 'Needs context', '#E8913A'],
  ] as const) {
    test(`${status}: its word in its colour, the PR, verify and the lists`, async ($, on) => {
      mock.env(on, { ATHENA_WORKER: 'lab-w', FORCE_HYPERLINK: '1' })
      engine(on)
      const ui = await mount($, report({ status, ...FULL }))
      expect(await ui.find({ type: 'Text', text: 'Pushed and opened the PR.' })).toBeDefined()
      expect((await ui.find({ type: 'Text', text: word }))?.props.color).toBe(color)
      expect(await ui.find({ type: 'Text', text: '∞ athena report' })).toBeDefined()
      expect((await ui.find({ type: 'Link' }))?.props).toEqual({ href: 'https://github.com/o/r/pull/6553' })
      expect(await ui.find({ type: 'Text', text: '#6553' })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: '797b679' })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: 'make test -> pass' })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: '• kept the frame for the model' })).toBeDefined()
      expect((await ui.find({ type: 'Text', text: '• needs a human look' }))?.props.color).toBe('warning')
      expect(await ui.find({ text: /ATHENA-REPORT/ })).toBeUndefined()
      await ui.unmount()
    })
  }

  test('malformed JSON keeps the engine drawing', async ($, on) => {
    mock.env(on, { ATHENA_WORKER: 'lab-w' })
    engine(on)
    const text = 'Done.\n\nATHENA-REPORT {"status":"DONE","pr":'
    const ui = await mount($, text)
    expect(await ui.find({ type: 'Text', text })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: '∞ athena report' })).toBeUndefined()
    expect(await ui.find({ type: 'Link' })).toBeUndefined()
    await ui.unmount()
  })
})
