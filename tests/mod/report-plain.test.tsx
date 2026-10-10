// A worker whose terminal draws no masked links (herdr without FORCE_HYPERLINK): the PR is plain `#n`, never the
// engine's `label URL` fallback, and a report naming another repo's PR is not linked into this one.
import type { On } from 'claude-code'
import type { Engine } from 'claude-code/testing'
import { expect, mock, test } from 'claude-code/testing'

function engine(on: On) {
  on('ui.render', { component: 'AssistantMessage' }, async ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>{e.props.text}</Text>
  })
  on('process.run', async ($, e, next) =>
    e.argv.join(' ') === 'git remote get-url origin' ? { value: { exitCode: 0, stdout: 'https://github.com/o/r.git\n', stderr: '' } } : next(e),
  )
}

const mount = ($: Engine, pr: string) =>
  $.ui.mount({
    plugin: 'athena',
    surface: 'terminal',
    component: 'AssistantMessage',
    props: { text: `Done.\n\nATHENA-REPORT ${JSON.stringify({ status: 'DONE', pr, decisions: [], concerns: [] })}`, isFirstOfReply: true },
  })

test('no masked links here: the PR draws as plain #n with no URL', async ($, on) => {
  mock.env(on, { ATHENA_WORKER: 'lab-w', TERM_PROGRAM: 'herdr', TERM: 'xterm-256color' })
  engine(on)
  const ui = await mount($, 'https://github.com/o/r/pull/6553')
  expect(await ui.find({ type: 'Text', text: '#6553' })).toBeDefined()
  expect(await ui.find({ type: 'Link' })).toBeUndefined()
  expect(await ui.find({ text: /https?:/ })).toBeUndefined()
  await ui.unmount()
})
