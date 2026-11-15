import { expect, mock, test } from 'claude-code/testing'

// The rule of the board's row, run by Claude Code's own kit: `claude plugin test <the plugin folder>`.
// The mod is loaded with the given `options`; below it the test answers what the helper prints (`process.run`), the time of
// the status line's marker (`fs.stat`) and the clock. The button is drawn whenever `board_link_row` or `board_link_status_line` is on, and
// whatever the status line shows: its link is an addition beside the button and never replaces it. Both off: nothing is drawn.

const START = '/work/shop-app'
const MARKER = '/data/seen/0123456789abcdef'
const TEN_MINUTES = 600_000
const BAND = { plugin: 'project-init', component: 'AbovePrompt', surface: 'terminal', props: { hasSurvey: false, bodyColumns: 100 } } as const

type Fixture = { url?: string | null; markerAt?: number | null; up?: boolean; opener?: string | null; hostileOpen?: string; name?: string }

const HOTKEY = '9' // a digit pressed in an empty prompt: not 1 to 3, the digits the owner answers a numbered question with

const KEY = 'ab'.repeat(32) // an invented board key: 64 lowercase hex characters

async function open($: any, on: any, fixture: Fixture = {}) {
  const clock = mock.clock(on, { now: 1_000_000_000_000 })
  const world = { markerAt: fixture.markerAt === undefined ? null : fixture.markerAt, helperRuns: 0, markerLooks: 0, argvs: [] as string[][], toasts: [] as string[] }
  const url = fixture.url === undefined ? `http://localhost:8800/${fixture.name ?? 'shop-app'}` : fixture.url

  on('ui.toast', (_: unknown, e: { text: string }) => {
    world.toasts.push(e.text)

    return { value: undefined }
  })
  on('process.run', (_: unknown, e: { argv: string[] }) => {
    world.argvs.push([...e.argv])

    if (e.argv.some(part => part.endsWith('open_board.py'))) {
      return { value: { exitCode: 0, stdout: `${fixture.opener ?? 'Opened the board in your browser.'}\n`, stderr: '' } }
    }

    world.helperRuns += 1

    // a call on `$` is answered `{ value }`
    return {
      value: {
        exitCode: 0,
        stdout: `${JSON.stringify({ state: '/home/state', url, up: fixture.up ?? true, port: 40111, data: '/data', seen_paths: [MARKER], status_line_seen: false, ...(fixture.hostileOpen ? { open: fixture.hostileOpen } : {}) })}\n`,
        stderr: '',
      },
    }
  })
  on('fs.stat', (_: unknown, e: { path: string }) => {
    if (e.path === MARKER) {
      world.markerLooks += 1
    }

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
    const row = await ui.find({ type: 'Button', text: /board/ })

    await ui.unmount()

    return row !== undefined
  }

  const tree = async () => {
    const ui = await $.ui.mount(BAND)
    const buttons = await ui.findAll({ type: 'Button' })
    const links = await ui.findAll({ type: 'Link' })
    const texts = await ui.findAll({ type: 'Text' })

    await ui.unmount()

    return { button: buttons[0], buttons, link: links[0], links, texts }
  }

  return { clock, world, isDrawn, tree, $ }
}

test('test_row_tree_has_one_button_no_link_and_no_key: the row is one Button, whose press opens the board; no Link, no address, no key', async ($, on) => {
  const { tree } = await open($, on)
  const { buttons, links, texts } = await tree()
  const drawn = JSON.stringify({ buttons, links, texts })

  expect(buttons).toHaveLength(1)
  expect(links).toHaveLength(0)
  expect(buttons[0]?.props?.label).toBe('board localhost:8800/shop-app')
  expect(buttons[0]?.props?.key ?? 'open-board').toBe('open-board')
  expect(drawn.includes(KEY)).toBe(false)
  expect(drawn.includes('?k=')).toBe(false)
  expect(drawn.includes('http')).toBe(false) // nothing in the row is an address with a scheme, so nothing prints one
  expect(drawn.includes('"href"')).toBe(false)
})

test('the Button has the digit hotkey, drawn plain so the row shows it (9: board ...), dim like the link was', async ($, on) => {
  const { tree } = await open($, on)
  const { button } = await tree()

  expect(button?.props?.hotkey).toBe(HOTKEY)
  expect(button?.props?.plain).toBe(true)
  expect(button?.props?.dimColor).toBe(true)
  expect(/^[0-9]$/.test(button?.props?.hotkey)).toBe(true)
  expect(['1', '2', '3', '0'].includes(button?.props?.hotkey)).toBe(false) // a survey's digits, and the ones a numbered answer starts with
})

test('while the board is not running the Button is still there and says so; pressing it asks the opener, which says it is not running', async ($, on) => {
  const { tree, world } = await open($, on, { up: false, opener: 'The board is not running; it starts with the next session in this project.' })
  const { buttons, links, button } = await tree()
  const ui = await $.ui.mount(BAND)

  expect(buttons).toHaveLength(1)
  expect(links).toHaveLength(0)
  expect(button?.props?.label).toBe('board localhost:8800/shop-app (not running)')
  await ui.press({ key: 'open-board' })
  await ui.unmount()
  expect(world.toasts).toEqual(['The board is not running; it starts with the next session in this project.'])
})

test('pressing the Button runs the opener on the session folder: a path and a folder on the command line, no key, and its one sentence is shown', async ($, on) => {
  const { tree, world } = await open($, on)
  const ui = await $.ui.mount(BAND)

  await ui.press({ key: 'open-board' })
  await ui.unmount()
  await tree()

  const opener = world.argvs.find(argv => argv.some(part => part.endsWith('open_board.py')))

  expect(opener).toBeDefined()
  expect(opener?.slice(0, 1)).toEqual(['python3'])
  expect(opener?.slice(-2)).toEqual(['--start', START])
  expect(opener?.join(' ').includes(KEY)).toBe(false)
  expect(opener?.join(' ').includes('http')).toBe(false)
  expect(world.toasts).toEqual(['Opened the board in your browser.'])
})

const ADDRESSES = {
  'a full address': 'open http://localhost:8800/shop-app on this machine',
  'a loopback address and port': 'go to 127.0.0.1:40111 now',
  'a short address with a key': `try localhost:8800/shop-app?k=${KEY}`,
}

for (const [label, said] of Object.entries(ADDRESSES)) {
  test(`what is toasted is never an address: ${label} in the opener's sentence is replaced by a plain one`, async ($, on) => {
    const { world } = await open($, on, { opener: said })
    const ui = await $.ui.mount(BAND)

    await ui.press({ key: 'open-board' })
    await ui.unmount()
    expect(world.toasts).toEqual(['The board could not be opened from here.'])
  })
}

test('a name too long for the row is cut in what is drawn, and the Button stays one', async ($, on) => {
  const { tree } = await open($, on, { name: 'a-project-whose-name-is-far-too-long-for-one-row-'.repeat(4) })
  const { buttons, button } = await tree()

  expect(buttons).toHaveLength(1)
  expect(button?.props?.label.length).toBeLessThanOrEqual(100 - 6)
  expect(button?.props?.label.endsWith('…')).toBe(true)
})

test('a helper that says an `open` (an older or a hostile one) is ignored: no key anywhere in what the row draws', async ($, on) => {
  const { tree } = await open($, on, { hostileOpen: `http://localhost:8800/shop-app?k=${KEY}` })
  const { buttons, links, texts } = await tree()

  expect(JSON.stringify({ buttons, links, texts }).includes(KEY)).toBe(false)
  expect(JSON.stringify({ buttons, links, texts }).includes('?k=')).toBe(false)
  expect(links).toHaveLength(0)
})

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

test('the row off and the status line on, the link seen a minute ago: the button is still drawn (the status line adds a link, it replaces nothing)', { options: { board_link_row: false, board_link_status_line: true } }, async ($, on) => {
  const { isDrawn } = await open($, on, { markerAt: 1_000_000_000_000 - 60_000 })

  expect(await isDrawn()).toBe(true)
})

test('the row off and the status line on, the link last seen eleven minutes ago: the button is drawn, as at every other time', { options: { board_link_row: false, board_link_status_line: true } }, async ($, on) => {
  const { isDrawn } = await open($, on, { markerAt: 1_000_000_000_000 - TEN_MINUTES - 60_000 })

  expect(await isDrawn()).toBe(true)
})

test('the button does not hide when the link first shows in the status line', { options: { board_link_row: false, board_link_status_line: true } }, async ($, on) => {
  const { clock, world, isDrawn } = await open($, on)

  expect(await isDrawn()).toBe(true)
  world.markerAt = 1_000_000_000_000 + 1_000
  await clock.advance(10_000)
  expect(await isDrawn()).toBe(true)
})

test('the button is drawn at every moment of a long run, whatever the status line shows or stops showing', { options: { board_link_row: false, board_link_status_line: true } }, async ($, on) => {
  const { clock, world, isDrawn } = await open($, on, { markerAt: 1_000_000_000_000 - 1_000 })

  expect(await isDrawn()).toBe(true)
  await clock.advance(TEN_MINUTES - 20_000)
  expect(await isDrawn()).toBe(true)
  await clock.advance(30_000)
  expect(await isDrawn()).toBe(true)
  world.markerAt = null
  await clock.advance(TEN_MINUTES)
  expect(await isDrawn()).toBe(true)
})

test('both on: the button is drawn, whatever the status line shows', { options: { board_link_row: true, board_link_status_line: true } }, async ($, on) => {
  const { isDrawn } = await open($, on, { markerAt: 1_000_000_000_000 - 1_000 })

  expect(await isDrawn()).toBe(true)
})

test('the row on and the status line off: the marker is not even looked at', { options: { board_link_row: true, board_link_status_line: false } }, async ($, on) => {
  const { clock, world, isDrawn } = await open($, on, { markerAt: 1_000_000_000_000 - 1_000 })

  await clock.advance(30_000)
  expect(await isDrawn()).toBe(true)
  expect(world.helperRuns).toBe(1)
  expect(world.markerLooks).toBe(0)
})

test('the row off and the status line on: the marker is not looked at either, the mod no longer reads it', { options: { board_link_row: false, board_link_status_line: true } }, async ($, on) => {
  const { clock, world, isDrawn } = await open($, on, { markerAt: 1_000_000_000_000 - 1_000 })

  await clock.advance(30_000)
  expect(await isDrawn()).toBe(true)
  expect(world.markerLooks).toBe(0)
})

test('only both switches off hides the button, and then at every moment', { options: { board_link_row: false, board_link_status_line: false } }, async ($, on) => {
  const { clock, isDrawn } = await open($, on, { markerAt: null })

  expect(await isDrawn()).toBe(false)
  await clock.advance(TEN_MINUTES + 60_000)
  expect(await isDrawn()).toBe(false)
})
