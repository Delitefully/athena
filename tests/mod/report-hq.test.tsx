// Outside a worker (no ATHENA_WORKER, and `athena whoami` names none: HQ or any other session) the line is the
// engine's to draw.
import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

/** Stands in for the engine's own drawing of the message, and for `athena whoami` outside any worktree. */
function engine(on: On) {
  on('ui.render', { component: 'AssistantMessage' }, async ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>{e.props.text}</Text>
  })
  on('process.run', async ($, e, next) =>
    e.argv[1] === 'whoami' ? { value: { exitCode: 1, stdout: '', stderr: '' } } : next(e),
  )
}

test('a session that is not a worker keeps the engine drawing', async ($, on) => {
  mock.env(on, { ATHENA_ROLE: 'hq' })
  engine(on)
  const text = `Done.\n\nATHENA-REPORT ${JSON.stringify({ status: 'DONE', decisions: [], concerns: [] })}`
  const ui = await $.ui.mount({ plugin: 'athena', surface: 'terminal', component: 'AssistantMessage', props: { text, isFirstOfReply: true } })
  expect(await ui.find({ type: 'Text', text: '∞ athena report' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text })).toBeDefined()
  await ui.unmount()
})
