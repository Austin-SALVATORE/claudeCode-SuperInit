import { expect, mock, test } from 'claude-code/testing'

// The rule of the board's row (release 2.3.0, part A), run by Claude Code's own kit: `claude plugin test <the plugin folder>`.
// The mod is loaded with the given `options`; below it the test answers what the helper prints (`process.run`), the time of
// the status line's marker (`fs.stat`) and the clock. The row is drawn when `board_link_row` is on, or when
// `board_link_status_line` is on and the marker is not younger than ten minutes; with both off nothing is drawn.

const START = '/work/shop-app'
const MARKER = '/data/seen/0123456789abcdef'
const TEN_MINUTES = 600_000
const BAND = { plugin: 'project-init', component: 'AbovePrompt', surface: 'terminal', props: { hasSurvey: false, bodyColumns: 100 } } as const

type Fixture = { url?: string | null; markerAt?: number | null }

async function open($: any, on: any, fixture: Fixture = {}) {
  const clock = mock.clock(on, { now: 1_000_000_000_000 })
  const world = { markerAt: fixture.markerAt === undefined ? null : fixture.markerAt, helperRuns: 0 }
  const url = fixture.url === undefined ? 'http://localhost:8800/shop-app' : fixture.url

  on('process.run', () => {
    world.helperRuns += 1

    // a call on `$` is answered `{ value }`
    return {
      value: {
        exitCode: 0,
        stdout: `${JSON.stringify({ state: '/home/state', url, up: true, data: '/data', seen_paths: [MARKER], status_line_seen: false })}\n`,
        stderr: '',
      },
    }
  })
  on('fs.stat', (_: unknown, e: { path: string }) => {
    if (e.path === MARKER && world.markerAt !== null) {
      return { value: { kind: 'file', size: 0, mtimeMs: world.markerAt, isLink: false } }
    }

    // a path that is not there: a stat that finds nothing reads as time 0 for the mod, as for the real thing that rejects
    return { value: { kind: 'other', size: 0, mtimeMs: 0, isLink: false } }
  })
  on('ui.render', ($: any, e: any) => {
    const { Text } = $.ui.resolve(e)

    return <Text>engine</Text>
  })
  on('session.start', (_: unknown, e: { cwd: string }) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: START, surface: 'terminal', isInteractive: true })
  await clock.settle()

  const isDrawn = async () => {
    const ui = await $.ui.mount(BAND)
    const row = await ui.find({ type: 'Text', text: /board/ })

    await ui.unmount()

    return row !== undefined
  }

  return { clock, world, isDrawn }
}

test('by default the row is drawn: nothing stored, nobody asked', async ($, on) => {
  const { isDrawn } = await open($, on)

  expect(await isDrawn()).toBe(true)
})

test('nothing is drawn where the helper has no address', async ($, on) => {
  const { isDrawn } = await open($, on, { url: null })

  expect(await isDrawn()).toBe(false)
})

test('both switches off: nothing is drawn, the owner chose so', { options: { board_link_row: false, board_link_status_line: false } }, async ($, on) => {
  const { isDrawn } = await open($, on)

  expect(await isDrawn()).toBe(false)
})

test('the row off and the status line on, the link never seen there: the row is drawn, the link is never lost', { options: { board_link_row: false, board_link_status_line: true } }, async ($, on) => {
  const { isDrawn } = await open($, on)

  expect(await isDrawn()).toBe(true)
})

test('the row off and the status line on, the link seen a minute ago: no row', { options: { board_link_row: false, board_link_status_line: true } }, async ($, on) => {
  const { isDrawn } = await open($, on, { markerAt: 1_000_000_000_000 - 60_000 })

  expect(await isDrawn()).toBe(false)
})

test('the row off and the status line on, the link last seen eleven minutes ago: the row is back', { options: { board_link_row: false, board_link_status_line: true } }, async ($, on) => {
  const { isDrawn } = await open($, on, { markerAt: 1_000_000_000_000 - TEN_MINUTES - 60_000 })

  expect(await isDrawn()).toBe(true)
})

test('the row hides within ten seconds of the link first showing in the status line', { options: { board_link_row: false, board_link_status_line: true } }, async ($, on) => {
  const { clock, world, isDrawn } = await open($, on)

  expect(await isDrawn()).toBe(true)
  world.markerAt = 1_000_000_000_000 + 1_000
  await clock.advance(10_000)
  expect(await isDrawn()).toBe(false)
})

test('the row comes back within ten minutes and ten seconds of the link last showing in the status line', { options: { board_link_row: false, board_link_status_line: true } }, async ($, on) => {
  const { clock, isDrawn } = await open($, on, { markerAt: 1_000_000_000_000 - 1_000 })

  expect(await isDrawn()).toBe(false)
  await clock.advance(TEN_MINUTES - 20_000)
  expect(await isDrawn()).toBe(false)
  await clock.advance(30_000)
  expect(await isDrawn()).toBe(true)
})

test('both on: the row stays, whatever the status line shows', { options: { board_link_row: true, board_link_status_line: true } }, async ($, on) => {
  const { isDrawn } = await open($, on, { markerAt: 1_000_000_000_000 - 1_000 })

  expect(await isDrawn()).toBe(true)
})

test('the row on and the status line off: the marker is not even looked at', { options: { board_link_row: true, board_link_status_line: false } }, async ($, on) => {
  const { clock, world, isDrawn } = await open($, on, { markerAt: 1_000_000_000_000 - 1_000 })

  await clock.advance(30_000)
  expect(await isDrawn()).toBe(true)
  expect(world.helperRuns).toBe(1)
})
