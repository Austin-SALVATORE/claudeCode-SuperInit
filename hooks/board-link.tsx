import type { Register } from 'claude-code'

// The board's short address, http://localhost:8800/<name>, as a row above the prompt (release 2.2.0, part C).
// It needs Claude Code 2.1.287 or later, which has mods; an older one ignores this file and every other hook of the plugin
// still works. A mod runs without Python and without Node, so what is shown is decided by one helper,
// status-board/board_link.py, which finds the project, the board and the name the way 2.1.0 does and prints the address or
// nothing. This file asks it and draws the answer. It sends no request (the helper reads files), writes nothing, and any
// failure draws nothing.
//
// Release 2.3.0: where the link shows is the owner's choice, two switches of the plugin's own (`userConfig` in plugin.json,
// the dialog when the plugin is installed, the /config rows, `pluginConfigs` in the user's settings):
//   board_link_row          (default on)   the row above the prompt, as in 2.2.x;
//   board_link_status_line  (default off)  the link in the status line, put there by /project-init:board-link.
// The row is drawn when the first is on, or when the second is on and the link has not been seen in the status line in the
// last ten minutes (the status line's filter touches a file each time it places the link; the helper says which file). Both
// off: nothing is drawn. So the link is never lost: a status line that stops showing it gives the row back by itself.
// The mod reads `options` once, in `register`: Claude Code reloads the mod with the new values when they change, from /config
// or in the settings file (spike of 15 Nov 2026), so no `config.set` hook and no re-read of the settings is needed.

const POLL_MS = 10_000 // how often the folders below are looked at: $.fs.stat, no process is started
const REFRESH_MS = 300_000 // the helper is started at least this often whatever the folders did: a server that died changes no file
const SEEN_MS = 600_000 // the link counts as shown in the status line for ten minutes after the filter last placed it

type Shown = { url: string; up: boolean }

export const register: Register = (on, options) => {
  const wantsRow = options?.board_link_row !== false // on unless the owner turned it off: the default, and a Claude Code that gave no options
  const wantsStatusLine = options?.board_link_status_line === true
  let shown: Shown | null = null
  let state: string | null = null // the status-board home the helper read: where a new board shows up
  let seenPaths: string[] = [] // the files whose times say when the status line last showed the link (from the helper)
  let isSeen = false // the status line showed the link in the last SEEN_MS
  let isAgent = false
  let seen = '' // the times of servers/ and boards/ when the helper last ran
  let lastRun = 0
  let isRunning = false
  let isStarted = false
  const isWanted = (): boolean => wantsRow || (wantsStatusLine && !isSeen)

  on('session.start', async ($, e, next) => {
    // Nobody is at the prompt under claude -p or the SDK: there is no band to draw, and nothing to ask for.
    if (!e.isInteractive || isStarted) {
      return next(e)
    }

    isStarted = true
    const start = e.cwd
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

    // Only a mod that waits for the status line looks at the marker: with the row kept on the rule does not need it.
    const looksAtMarker = async () => {
      if (wantsRow || !wantsStatusLine || seenPaths.length === 0) {
        return
      }

      let newest = 0

      for (const path of seenPaths) {
        newest = Math.max(newest, await $.fs.stat(path).then(stat => stat.mtimeMs, () => 0))
      }

      const isNow = newest > 0 && (await $.clock.now()) - newest < SEEN_MS

      if (isNow !== isSeen) {
        isSeen = isNow
        $.ui.invalidate('ui.render')
      }
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
          seen_paths?: string[]
          status_line_seen?: boolean
        } | null
        const fresh: Shown | null = said?.url ? { url: said.url, up: said.up === true } : null

        state = typeof said?.state === 'string' ? said.state : state
        isAgent = isAgent || said?.agent === true
        seenPaths = Array.isArray(said?.seen_paths) ? said.seen_paths.filter(path => typeof path === 'string') : []

        if (seenPaths.length === 0) {
          isSeen = said?.status_line_seen === true // no file to look at: the helper's own reading stands
        }

        seen = await timesOf()
        await looksAtMarker()

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
        await looksAtMarker() // a stat of one file, no process: the row hides within one poll of the link first showing in the status line

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

    const { Box, Text, Link } = $.ui.resolve(e)
    // Before Claude Code 2.1.323 every mod shared one drawing in the band, and a tree without the result of `await next(e)`
    // in it replaced what the mods after it drew. From 2.1.323 that child draws nothing of other mods' rows. For the tree to
    // stay valid on the older releases, no Box of ours carries width, height, minWidth, minHeight, position, top, left, right,
    // bottom, display or overflow (the docs: troubleshoot, "Another mod's band shows and yours doesn't").
    const theirs = await next(e)

    // The content sits at the right end of the row, beside the engine's [-]: the inner Box is a row stretched across the
    // band by the column Box around it, and `justifyContent="flex-end"` puts what it holds at its end. That follows the
    // terminal's width by itself (a resize moves it); nothing here counts columns or pads with spaces. One Text holds the
    // label, the link and the marker, truncated at its end: a name too long for the row is cut with an ellipsis, and the
    // row stays one row with the [-] where it was (the link keeps the whole address; only what is drawn is cut).
    return (
      <Box flexDirection="column">
        <Box justifyContent="flex-end">
          <Text wrap="truncate-end">
            <Text dimColor>board </Text>
            <Link href={shown.url} />
            {shown.up ? null : <Text dimColor> (not running)</Text>}
          </Text>
        </Box>
        {theirs}
      </Box>
    )
  })
}
