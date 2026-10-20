// A worker herdr resumed bare after a restart (`claude --resume <id>`: no ATHENA_WORKER in its environment or
// settings) is still a worker: `athena whoami` finds it by its worktree in the ledger, and its report draws as a block.
import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

const asked: string[][] = []

/** Stands in for the engine's own drawing, the worktree's origin remote, and `athena whoami` in the worktree. */
function engine(on: On) {
  on('ui.render', { component: 'AssistantMessage' }, async ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>{e.props.text}</Text>
  })
  on('process.run', async ($, e, next) => {
    if (e.argv[1] === 'whoami') {
      asked.push([...e.argv])
      return { value: { exitCode: 0, stdout: 'lab-w\n', stderr: '' } }
    }
    if (e.argv.join(' ') === 'git remote get-url origin') {
      return { value: { exitCode: 0, stdout: 'git@github.com:o/r.git\n', stderr: '' } }
    }
    return next(e)
  })
}

test('no ATHENA_WORKER, but whoami names the worktree: the report draws as a completion block', async ($, on) => {
  mock.env(on, { TERM_PROGRAM: 'herdr' }) // the environment herdr's bare resume leaves: no ATHENA_WORKER
  engine(on)
  const text = `Done.\n\nATHENA-REPORT ${JSON.stringify({ status: 'DONE', pr: 'https://github.com/o/r/pull/46', decisions: [], concerns: [] })}`
  const ui = await $.ui.mount({ plugin: 'athena', surface: 'terminal', component: 'AssistantMessage', props: { text, isFirstOfReply: true } })
  expect(await ui.find({ type: 'Text', text: '∞ athena report' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: 'Done' })).toBeDefined()
  expect(await ui.find({ text: /ATHENA-REPORT/ })).toBeUndefined()
  expect(asked.length).toBe(1)
  expect(asked[0][0].endsWith('/bin/athena')).toBe(true)
  await ui.unmount()
})
