// Outside a worker (no ATHENA_WORKER: HQ or any other session) the line is the engine's to draw.
import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'

/** Stands in for the engine's own drawing of the message: its text, as handed down. */
function engine(on: On) {
  on('ui.render', { component: 'AssistantMessage' }, async ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>{e.props.text}</Text>
  })
}

test('a session that is not a worker keeps the engine drawing', async ($, on) => {
  engine(on)
  const text = `Done.\n\nATHENA-REPORT ${JSON.stringify({ status: 'DONE', decisions: [], concerns: [] })}`
  const ui = await $.ui.mount({ plugin: 'athena', surface: 'terminal', component: 'AssistantMessage', props: { text, isFirstOfReply: true } })
  expect(await ui.find({ type: 'Text', text: '∞ athena report' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text })).toBeDefined()
  await ui.unmount()
})
