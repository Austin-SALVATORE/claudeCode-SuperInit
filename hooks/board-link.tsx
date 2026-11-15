import type { Register } from 'claude-code'

// The board's short address, http://localhost:8800/<name>, as a row above the prompt (release 2.2.0, part C).
// It needs Claude Code 2.1.287 or later, which has mods; an older one ignores this file and every other hook of the plugin
// still works. A mod runs without Python and without Node, so what is shown is decided by one helper,
// status-board/board_link.py, which finds the project, the board and the name the way 2.1.0 does and prints the address or
// nothing. This file asks it and draws the answer. It sends no request (the helper reads files), writes nothing, and any
// failure draws nothing.

const POLL_MS = 10_000 // how often the two folders below are looked at: $.fs.stat, no process is started
const REFRESH_MS = 300_000 // the helper is started at least this often whatever the folders did: a server that died changes no file

type Shown = { url: string; up: boolean }

export const register: Register = on => {
  let shown: Shown | null = null
  let state: string | null = null // the status-board home the helper read: where a new board shows up
  let isAgent = false
  let seen = '' // the times of servers/ and boards/ when the helper last ran
  let lastRun = 0
  let isRunning = false
  let isStarted = false

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
        const said = JSON.parse(done.stdout.trim() || 'null') as { state?: string; url?: string | null; up?: boolean; agent?: boolean } | null
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
    if (shown === null || e.props.hasSurvey) {
      return next(e)
    }

    const { Box, Text, Link } = $.ui.resolve(e)

    return (
      <Box>
        <Text dimColor>board </Text>
        <Text>
          <Link href={shown.url} />
        </Text>
        {shown.up ? null : <Text dimColor> (not running)</Text>}
      </Box>
    )
  })
}
