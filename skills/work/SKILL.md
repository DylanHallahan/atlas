---
name: work
description: "Track work with the work CLI when starting or resuming work, taking a brief, finishing, waiting on someone, or asking another agent."
---

# Work

Run from your own team's folder. Outside a Herdr pane, add `--as AGENT` after the subcommand.
Until the PATH install, call `~/teams/atlas/deploy-dev/bin/work`.
Use the returned item id in later commands. The examples use `atlas:work-cli-guide`.
When taking a brief, reuse its `Item:` id. Workers never run `add` for that work.

- After restart or compaction, read your open items: `~/teams/atlas/deploy-dev/bin/work mine`.
- Start new work with an owner and a next action: `~/teams/atlas/deploy-dev/bin/work add "Work CLI guide" --owner atlas-lead --next "Draft the guide"`.
- On a status change: `~/teams/atlas/deploy-dev/bin/work move atlas:work-cli-guide doing --next "Check the examples"`.
- Record progress: `~/teams/atlas/deploy-dev/bin/work note atlas:work-cli-guide "Examples checked in scratch"`.
- At the merge, enter proving: `~/teams/atlas/deploy-dev/bin/work move atlas:work-cli-guide proving --proof "AR totals on production match the import" --stands "On dev"`. Update where it stands: `... move atlas:work-cli-guide proving --stands "In production"`.
- Finish with a report path or PR link and the proof's result: `~/teams/atlas/deploy-dev/bin/work close atlas:work-cli-guide done --note "records/reports/work-cli-guide.md" --proof "Read the totals on production, 10:05 AM ET"`. With nothing to prove, pass `--nothing-to-prove "<reason>"` instead.
- Hand an item to another owner, keeping its status: `~/teams/atlas/deploy-dev/bin/work move atlas:work-cli-guide doing --owner atlas-product-lead`.
- Waiting on Dylan, with the ask block's Default line: `~/teams/atlas/deploy-dev/bin/work move atlas:work-cli-guide waiting --waiting-on Dylan --default "Holds."`.
- A lead briefing a worker: `~/teams/atlas/deploy-dev/bin/work brief atlas-writer --item atlas:work-cli-guide`.
- Asking another agent: `~/teams/atlas/deploy-dev/bin/work ask atlas-product-lead "Review the guide" --next "Check the wording"`.

`move` takes todo, doing, waiting or proving. `close` takes done, with a report path or PR link and `--proof` or `--nothing-to-prove`, or dropped, with the reason.
Proving is merged work not yet proved where its user gets it. It goes stale after 3 days.
`--waiting-on` sets the status to waiting. Write the wait as "who: what (since when)", or an item id. It counts as waiting on Dylan when who is Dylan.
Waiting on Dylan also needs `--default`: "Holds." or "Goes ahead: <letter and what happens>, <when>.".
`brief` writes a brief skeleton and its child item. Fill in the brief before you start the worker.
`ask` records an item. Neither command sends. Send them yourself: the ask by SendMessage to that agent, and the brief by starting the worker on it.
Every brief, report, ask report and PR description carries `Item: <id>` in its header, next to the From line; that line is how asks and reports join an item.
`--link` is stored exactly as given: quote a value to keep `~` or `[[...]]`.
`--set key=value`, on `add` and `move`, writes your own frontmatter key. Core keys are refused and other keys stay.
Leads: `~/teams/atlas/deploy-dev/bin/work stale` lists your team's open items untouched past their limit: 4 hours doing, 24 hours todo or waiting.
