"""Start one deploy: create the database, start the herdr, registry, transcript, STATUS, reports and items pollers, serve on
127.0.0.1. With --reindex, drop and create again the index tables of ATLAS_DB and exit."""
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

from . import db, herdr, items, messages, raincli, reports, review, status, transcripts, web
from .structure import Structure

REPO = Path(__file__).resolve().parent.parent


def git(*args):
    try:
        return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True,
                              timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def main():
    port, path = os.environ.get("ATLAS_PORT"), os.environ.get("ATLAS_DB")
    if sys.argv[1:] == ["--reindex"]:
        if not path:
            sys.exit("atlas: set ATLAS_DB")
        db.reindex(path)
        return print(f"atlas: index tables of {path} dropped and created again; the kept tables stay")
    if not port or not path:
        sys.exit("atlas: set ATLAS_PORT and ATLAS_DB")
    binary = os.environ.get("ATLAS_HERDR") or "herdr"
    hc_binary = os.environ.get("ATLAS_HC") or "hc"
    structure = Structure(os.environ.get("ATLAS_STRUCTURE") or REPO / "structure.toml")
    # Read once at start: the footer names what this process runs, and the branch is the deploy (main or dev).
    app = SimpleNamespace(db=path, structure=structure, herdr=binary, hc=hc_binary,
                          raincli=os.environ.get("ATLAS_RAINCLI") or "raincli",
                          typist=os.environ.get("ATLAS_TYPIST") or None,
                          devices={d.strip() for d in os.environ.get("ATLAS_TYPIST_DEVICES", "").split(",") if d.strip()},
                          # tailscaled runs as root; a check instance only may name another uid. The deploy unit refuses it.
                          serve_uid=int(os.environ.get("ATLAS_SERVE_UID") or 0),
                          commit=git("rev-parse", "--short", "HEAD"),
                          deploy=git("rev-parse", "--abbrev-ref", "HEAD"))

    app.review = review.Review(db.connect(path), structure)
    new = db.create(path)
    db.scrub(db.connect(path))
    pollers = [herdr.StatePoller(db.connect(path), structure, hc_binary), herdr.ScreensPoller(db.connect(path), hc_binary),
               transcripts.RegistryPoller(db.connect(path)), messages.TranscriptPoller(db.connect(path), structure, new),
               status.StatusPoller(db.connect(path), structure, app.typist), reports.ReportsPoller(db.connect(path), structure, app.review),
               items.ItemsPoller(db.connect(path), structure), raincli.Poller(db.connect(path), app.raincli), app.review]
    for p in pollers:
        threading.Thread(target=herdr.every, args=(p.interval, p.poll), name=type(p).__name__, daemon=True).start()
    print(f"atlas: {app.deploy} at {app.commit} on 127.0.0.1:{port}, database {path}", flush=True)
    try:
        web.serve(app, int(port))
    except KeyboardInterrupt:
        pass


main()
