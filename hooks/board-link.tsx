import type { Register } from 'claude-code'

// The board, as a row above the prompt (a single button since release 2.4.0).
//
// Release 2.4.0: the row is ONE Button, `9: board localhost:8800/<name>` (plus ` (not running)` when the server is down), and its press
// runs the plugin's opener (a Python script beside the helper), which gives the owner's browser a token for that board and that run in a NEW tab. There
// is no Link: a board is opened from Claude Code for each browser tab, so that no program that takes a stopped board's port can open it as
// the owner (a link only reaches a page that asks for the button). Nothing in the row is an address with a scheme, and
// nothing is printed by a terminal that draws no hyperlinks. The Button is pressed by a mouse click (in fullscreen rendering), by Enter
// once the band has the keyboard (ctrl+x then Tab, then Tab to walk to it), or by its digit, typed into an EMPTY prompt: the digit is then
// consumed, not typed (measured with Claude Code 2.1.325 and the digit 1; the same in the classic renderer). The digit is 9, not 1 to 3,
// which the owner starts a reply to a numbered question with, and not 0, a survey's; where another mod's Button in the band has the same
// digit, Claude Code presses neither (the reference: "Two plugins on one hotkey: it presses neither"): the click and Enter still work.
// It needs Claude Code 2.1.287 or later, which has mods; an older one ignores this file and every other hook of the plugin
// still works. A mod runs without Python and without Node, so what is shown is decided by one helper,
// status-board/board_link.py, which finds the project, the board and the name the way 2.1.0 does and prints the address or
// nothing. This file asks it and draws the answer. It sends no request (the helper reads files), writes nothing, and any
// failure draws nothing.
//
// What is shown is the owner's choice, two switches of the plugin's own (`userConfig` in plugin.json, the dialog when the plugin is
// installed, the /config rows, `pluginConfigs` in the user's settings):
//   board_link_row          (default on)   the button above the prompt;
//   board_link_status_line  (default off)  a link in the status line, put there by /project-init:board-link.
// The button is drawn whenever EITHER is on: the status line's link is an addition beside it and never replaces it (that link only
// reaches a page that asks for the button, which is how a board is opened). Only both off hides the button, and then nothing is drawn.
// So this mod does not look at the status line at all: what the status line shows, or stops showing, changes nothing here.
// The mod reads `options` once, in `register`: Claude Code reloads the mod with the new values when they change, from /config
// or in the settings file, so no `config.set` hook and no re-read of the settings is needed.

const POLL_MS = 10_000 // how often the folders below are looked at: $.fs.stat, no process is started
const REFRESH_MS = 300_000 // the helper is started at least this often whatever the folders did: a server that died changes no file

type Shown = { url: string; up: boolean }

const HOTKEY = '9' // the digit that presses the row's Button from an empty prompt: see the header
const ELLIPSIS = '…'
const NOT_RUNNING = ' (not running)'
const GENERIC = 'The board could not be opened from here.'
const HOLDS_AN_ADDRESS = /https?:|localhost|127\.0\.0\.1|\[::1\]|[?]k=/i // what a toast must never hold

// `board localhost:8800/shop-app`, the address without its scheme, cut with an ellipsis where the row has no room. Only what is drawn is
// cut; the Button's press never takes the name from the label.
function labelFor(shown: Shown, columns: number): string {
  const address = shown.url.replace(/^https?:\/\//, '')
  const suffix = shown.up ? '' : NOT_RUNNING
  const room = Math.max(12, columns - 6) - 'board '.length - suffix.length - HOTKEY.length - 2 // the engine's [-] and `9: `
  const cut = address.length > room ? `${address.slice(0, Math.max(1, room - 1))}${ELLIPSIS}` : address

  return `board ${cut}${suffix}`
}

export const register: Register = (on, options) => {
  const wantsRow = options?.board_link_row !== false // on unless the owner turned it off: the default, and a Claude Code that gave no options
  const wantsStatusLine = options?.board_link_status_line === true
  let shown: Shown | null = null
  let state: string | null = null // the status-board home the helper read: where a new board shows up
  let isAgent = false
  let seen = '' // the times of servers/ and boards/ when the helper last ran
  let lastRun = 0
  let isRunning = false
  let isStarted = false
  let startFolder = '' // the folder the session was started in: what the opener is asked about
  const isWanted = (): boolean => wantsRow || wantsStatusLine

  on('session.start', async ($, e, next) => {
    // Nobody is at the prompt under claude -p or the SDK: there is no band to draw, and nothing to ask for.
    if (!e.isInteractive || isStarted) {
      return next(e)
    }

    isStarted = true
    const start = e.cwd
    startFolder = start
    const timesOf = async (): Promise<string> => {
      if (state === null) {
        return ''
      }

      const times: number[] = []

      for (const folder of ['servers', 'boards']) {
        times.push(await $.fs.stat(`${state}/${folder}`).then(stat => stat.mtimeMs, () => 0))
      }

      return times.join(',')
    }

    const refresh = async (isFirst: boolean) => {
      if (isRunning) {
        return
      }

      isRunning = true

      try {
        lastRun = await $.clock.now()
        const sessionId = await $.session.id().catch(() => undefined)
        const argv = ['python3', `${$.plugin.root}/status-board/board_link.py`, '--start', start]

        if (!isFirst) {
          argv.push('--no-agent-check') // told once, at the start: the dispatch listing is not asked again
        }

        const done = await $.process.run(argv, { stdin: JSON.stringify({ cwd: start, session_id: sessionId }), timeoutMs: 5000 })
        const said = JSON.parse(done.stdout.trim() || 'null') as {
          state?: string
          url?: string | null
          up?: boolean
          agent?: boolean
        } | null
        const fresh: Shown | null = said?.url ? { url: said.url, up: said.up === true } : null

        state = typeof said?.state === 'string' ? said.state : state
        isAgent = isAgent || said?.agent === true
        seen = await timesOf()

        if (fresh?.url !== shown?.url || fresh?.up !== shown?.up) {
          shown = fresh
          $.ui.invalidate('ui.render')
        }
      } catch {
        // draws nothing; the next poll tries again
      } finally {
        isRunning = false
      }
    }

    void refresh(true)

    $.clock.every(POLL_MS, () => {
      if (isAgent) {
        return
      }

      void (async () => {
        const isChanged = (await timesOf()) !== seen
        const isDue = (await $.clock.now()) - lastRun > REFRESH_MS

        // A run that said nothing (no python3, a broken folder) leaves `state` null and waits REFRESH_MS like any quiet run:
        // asking again at every poll would start a process 360 times an hour in a session that can never draw.
        if (isChanged || isDue) {
          await refresh(false)
        }
      })()
    })

    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (shown === null || e.props.hasSurvey || !isWanted()) {
      return next(e)
    }

    const { Box, Button } = $.ui.resolve(e)

    // The press (release 2.4.0): the board keeps its data behind a key that only the owner's account can read, and the owner's browser gets
    // a token for that board and that run, once for each tab, from the opener, which this press starts. The key is in no link, no address
    // and nothing drawn here: this module never reads it, holds it or passes it; the opener is given the project folder and prints one
    // sentence, which is all that is shown of it, and never an address (a sentence that held one is not shown).
    const openBoard = async () => {
      try {
        const done = await $.process.run(['python3', `${$.plugin.root}/status-board/open_board.py`, '--start', startFolder], { timeoutMs: 20_000 })
        const said = done.stdout.trim().split('\n')[0]?.slice(0, 300) ?? ''

        $.ui.toast(said && !HOLDS_AN_ADDRESS.test(said) ? said : GENERIC)
      } catch {
        $.ui.toast(GENERIC)
      }
    }
    // Before Claude Code 2.1.323 every mod shared one drawing in the band, and a tree without the result of `await next(e)`
    // in it replaced what the mods after it drew. From 2.1.323 that child draws nothing of other mods' rows. For the tree to
    // stay valid on the older releases, no Box of ours carries width, height, minWidth, minHeight, position, top, left, right,
    // bottom, display or overflow (the docs: troubleshoot, "Another mod's band shows and yours doesn't").
    const theirs = await next(e)
    const columns = typeof e.props.bodyColumns === 'number' ? e.props.bodyColumns : 80

    // The Button sits at the right end of the row, beside the engine's [-]: the inner Box is a row stretched across the band by the
    // column Box around it, and `justifyContent="flex-end"` puts what it holds at its end. That follows the terminal's width by itself
    // (a resize moves it); nothing here pads with spaces. `plain` draws `9: board ...` (the digit in the accent colour) so the row says
    // what to press; `dimColor` keeps it as quiet as the link was, full strength under the pointer or the focus.
    return (
      <Box flexDirection="column">
        <Box justifyContent="flex-end">
          <Button key="open-board" plain dimColor hotkey={HOTKEY} label={labelFor(shown, columns)} onPress={openBoard} />
        </Box>
        {theirs}
      </Box>
    )
  })
}
