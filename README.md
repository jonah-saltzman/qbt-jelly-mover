# qbt-jelly-mover

Watches qBittorrent for completed downloads and files them into a Jellyfin
library with correct naming, using `claude --model sonnet -p` for the one part
that needs judgment: identifying the media and producing a rename/move plan.

For each completed torrent it:

1. stops the torrent,
2. asks Claude to classify it (`movie` / `tv` / `other`) and, for media, to
   produce a Jellyfin-convention move plan (matching an existing library
   folder when one exists),
3. validates the plan deterministically (no path escapes, no overwrites,
   every file accounted for) and executes it with `mv`,
4. removes the torrent from qBittorrent (entry only, never files).

Non-media torrents are left stopped in qBittorrent. Low-confidence
classifications are moved whole into `NEEDS_REVIEW_DIR` for a human to sort
(or left in place if that setting is empty).

Junk files (release-group `.txt`/`.nfo`/`.url` notes, sample videos,
"screens"/"proof" images, checksums) never enter the library: Claude lists
them separately and they are moved into a per-torrent subfolder of
`RECYCLE_DIR` (default `<downloads>/.recycle`). The library's `extras/`
folder is reserved for real bonus content (trailers, featurettes, deleted
scenes). Empty the recycle bin yourself now and then.

All filesystem actions are moves; nothing is ever copied-and-kept or
deleted. The only "deletion" is `rmdir` of the empty folder husk a torrent
leaves behind (and that can be turned off with `CLEANUP_EMPTY_DIRS=false`).

## Token efficiency

- Claude is called **once per completed torrent**, never while polling.
- The call is nearly non-agentic: WebSearch is the only tool offered (used
  only to confirm an unfamiliar title/year), no settings or CLAUDE.md loaded
  (`--setting-sources ""` + `--strict-mcp-config`), a ~40-line replacement
  system prompt, no session persisted, and `--json-schema` for a validated
  structured reply.
- The prompt contains only the torrent name, its file list, and a locally
  pre-filtered shortlist of existing library folders that share a meaningful
  token with the torrent name (so the library listing isn't sent wholesale).
- `--max-budget-usd` caps each call. A typical call costs ~$0.05.

Everything else (polling, stopping, validating, moving, removing) is plain
Python stdlib with no LLM involvement.

## Observability

Each call writes a **trajectory transcript** next to the state file, named for
the torrent hash (`<STATE_FILE dir>/<hash>.log`, overridable with
`TRAJECTORY_DIR`). It records the exact prompt the model saw, its thinking,
any web searches it ran, its final answer, and the validated plan plus cost:

```sh
# review how a given torrent was handled
less ~/.local/state/qbt-jelly-mover/<hash>.log
# or, find the hash by name from the state file
python3 -c 'import json;print("\n".join(f"{h}  {e[\"name\"]}" for h,e in json.load(open(__import__("os").path.expanduser("~/.local/state/qbt-jelly-mover/state.json"))).items()))'
```

The file is rewritten each time that torrent is (re)processed, so it always
reflects the latest run -- including failures, where the transcript is the
quickest way to see what the model did. Set `TRAJECTORY_DIR=off` to disable.

## Setup

```sh
cp env.example .env   # fill in QBT_API_KEY etc.
./mover.py --once --dry-run   # see what it would do, change nothing
./mover.py                    # run in the foreground
```

`--dry-run` still calls Claude and prints the full move plan, but performs no
stops, moves, or removals.

Requirements: python3 (stdlib only), `claude` CLI logged in, the NAS shares
mounted (the script refuses to start if a configured directory is missing —
important so a down NFS mount can't cause mishandling).

## Install as a daemon (systemd)

```sh
sudo cp qbt-jelly-mover.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qbt-jelly-mover
journalctl -u qbt-jelly-mover -f   # watch it work
```

The unit runs as your user (the claude CLI uses your existing login) and
reads `.env` from this directory. If you move the repo, update the paths in
the unit file. Use an absolute `CLAUDE_BIN` in `.env` (systemd's PATH won't
include `~/.local/bin`).

## Configuration

See `env.example` for all settings. Notable:

| Setting | Meaning |
|---|---|
| `QBT_SAVE_PATH_PREFIX` / `LOCAL_DOWNLOADS_DIR` | qBittorrent runs on the NAS, so the API reports NAS paths (`/downloads/...`). These two map them to the local mount (`/mnt/downloads/...`). |
| `NEEDS_REVIEW_DIR` | Destination for low-confidence media. Empty = leave in downloads and mark failed. |
| `RECYCLE_DIR` | Junk files are moved here (per-torrent subfolders), never deleted. Empty = `<LOCAL_DOWNLOADS_DIR>/.recycle`, which keeps junk moves on the same NFS export (instant renames). |
| `QBT_CATEGORY` | Only process torrents in this category. Empty = all. |
| `MAX_ATTEMPTS` | After this many failures a torrent is parked; delete its entry from `STATE_FILE` to retry. |
| `TRAJECTORY_DIR` | Where per-torrent `<hash>.log` transcripts go. Empty = alongside `STATE_FILE`; `off` = disable. |

`test.env` (gitignored, see `env.example` for shape) points the library at a
scratch directory on the VM for safe end-to-end testing against the real
qBittorrent instance: `./mover.py --env test.env --once`.

## Notes

- `/mnt/downloads` and `/mnt/jelly` are separate NFS exports, so a "move"
  between them necessarily streams the data through this machine (`mv`
  handles it transparently). If you ever export a common parent (or run this
  on the NAS itself), moves become instant server-side renames.
- State lives in `STATE_FILE` keyed by torrent hash, so the daemon never
  re-asks Claude about a torrent it already classified (`skipped` /
  `needs_review` / parked-failed torrents stay in qBittorrent without
  burning tokens each poll).
- qBittorrent v5 API-key auth (`Authorization: Bearer`) is assumed; the
  stop call falls back to v4's `pause` automatically.
