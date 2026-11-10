# Atlas

A read-only window onto the agent teams on the herdr box: it indexes herdr panes, Claude and Codex transcripts and each team's records into one SQLite database, and serves a web view on the tailnet.

- One branch, `main`, and one deploy on the box (Dylan, 2026-10-30 5:18 PM ET).
- Records (briefs, specs, reports, reviews) live in `~/teams/atlas/records/`, not here.
- Architecture: `docs/architecture/`.

## Run
Python 3.14, standard library only, no build step. From the repo root:

```
ATLAS_PORT=8790 ATLAS_DB=/path/to/atlas.db python3 -m atlas
```

- `ATLAS_PORT` (required): port on `127.0.0.1`; the service never binds another address.
- `ATLAS_DB` (required): the SQLite file. The deploy uses `~/teams/atlas/data/atlas-dev.db`. A restart keeps it. `python3 -m atlas --reindex` with the service stopped drops and rebuilds only its index tables, and keeps `observations`, `first_reads` and `last_look`; never delete the file.
- `ATLAS_STRUCTURE`: the structure file, default `structure.toml` here.
- Pages: each team's `artifacts` folder, beside its `items` folder, is served at `/a/<team>/<path>` (files of the page's known types, at most 5 MB, symlinks and hidden names never; anything else answers 404) and listed on the Teams tab from each page's `<title>` and `<meta name="author">`. Every page carries `Content-Security-Policy: sandbox allow-scripts` and a policy that lets it load only its own team's files, so its scripts run in an opaque origin, can never use the typist's send token, and cannot fetch, post or load from any other host. `/mocks/<path>` answers 301 to `/a/atlas/<path>`.
- `ATLAS_TYPIST`: the one Tailscale login `/api/whoami` marks as the typist, compared exactly with the `Tailscale-User-Login` header `tailscale serve` attaches. Unset or empty means nobody.
- `ATLAS_TYPIST_DEVICES`: comma-separated stable node IDs (`Node.StableID`) of the devices the typist may type from, resolved per request with `tailscale whois` on the last `X-Forwarded-For` address. An ID survives a rename in the admin console; a name does not. Unset or empty means no device qualifies.
- `ATLAS_SERVE_UID`: for a check instance only. The Tailscale headers count only when the client end of the connection belongs to this uid, by default 0 (tailscaled runs as root). A check instance may name its own uid to test the typist path. The deploy unit never sets it, and refuses to start when it is set.
- `ATLAS_HERDR`: the herdr binary, default `herdr`. A herdr that fails shows in source health; the process stays up.
- `ATLAS_CLAUDE_STORES`, `ATLAS_CODEX_STORE`, `ATLAS_REGISTRIES`: colon-separated roots, default the three Claude `projects` stores, `~/.codex/sessions` and the three `sessions` registries. A store that cannot be read freezes the message list until it can.

It serves the page at `/` (its files under `/static/`), `GET /api/state` (agents, declared places, team names, source health, commit, deploy), `GET /api/screens` (each live pane's line lengths; with `?pane=<id>`, that pane's visible text; with `?text=1`, every held screen's text, for the agent grid at `/agents`) `GET /api/messages` (messages between agents in the last 24 hours, newest first, with `frozen` and the transcript sources' health), `GET /api/briefing` (each team's three STATUS lines, what changed since this login's last look, and what needs Dylan), `GET /api/shipped-picture?team=<key>` (the `.png` a team's Last shipped line names, inside its folders only) and `POST /api/seen` (this login's last look is now), each GET with an ETag. The deploy runs as the user unit in `deploy/`; see `docs/architecture/` for ports and paths.
