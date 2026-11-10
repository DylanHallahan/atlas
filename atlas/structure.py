"""The declared structure, structure.toml, read again whenever its mtime changes."""
import os
import pwd
import tomllib

from . import db


def expand(path):
    """~ in a structure.toml path means the box owner's home, never the caller's $HOME, so every caller resolves alike."""
    if path == "~" or path.startswith("~/"):
        path = pwd.getpwuid(os.getuid()).pw_dir + path[1:]
    return path


def check(data):
    """Raise ValueError unless the file has the shape Structure reads: [[agents]] tables, each with a name, and
    [teams.<key>] tables whose folders are a list of paths and whose workspace, name, status, decisions and reports are
    strings."""
    agents = data.get("agents", [])
    if not isinstance(agents, list) or not all(isinstance(a, dict) for a in agents):
        raise ValueError("agents must be [[agents]] tables")
    if not all(isinstance(a.get("name"), str) and a["name"] for a in agents):
        raise ValueError("every [[agents]] entry needs a name")
    teams = data.get("teams", {})
    if not isinstance(teams, dict) or not all(isinstance(t, dict) for t in teams.values()):
        raise ValueError("teams must be [teams.<key>] tables")
    for key, t in teams.items():
        folders = t.get("folders", [])
        if not isinstance(folders, list) or not all(isinstance(f, str) and f for f in folders):
            raise ValueError(f"teams.{key}.folders must be a list of paths")
        if not isinstance(t.get("workspace", ""), str):
            raise ValueError(f"teams.{key}.workspace must be a string")
        for field in ("name", "status", "decisions", "reports"):
            if not isinstance(t.get(field, ""), str):
                raise ValueError(f"teams.{key}.{field} must be a string")


class Structure:
    def __init__(self, path):
        self.path = path
        self.mtime = None
        self.data = {}
        self.tied = []

    def declared(self):
        """Declared agents by herdr name, in file order."""
        return {a["name"]: a for a in self.data.get("agents", []) if a.get("name")}

    def team_names(self):
        """Each team's display name by key, for the page; a team without a name shows its key."""
        return {key: t.get("name") or key for key, t in self.data.get("teams", {}).items()}

    def lead(self, team):
        """The team's agent with kind team-lead; for the chief team, the agent with kind chief. A team with no team-lead
        and exactly one declared agent has that agent as its lead; any other case is None. The agent keeps its own kind."""
        kind = "chief" if team == "chief" else "team-lead"
        mine = {n: a for n, a in self.declared().items() if a.get("team") == team}
        return next((n for n, a in mine.items() if a.get("kind") == kind), None) or (
            next(iter(mine)) if kind == "team-lead" and len(mine) == 1 and not any(a.get("kind") == kind for a in mine.values()) else None)

    def team_by_folder(self, cwd):
        """The team whose folders hold cwd, by whole path components; the longest match wins. Teams tied at the longest
        length give no team, and self.tied names them.
        Both sides are resolved the same way (~ expanded, symlinks followed), so two spellings of a directory agree."""
        self.tied = []
        if not cwd:
            return None
        cwd, matches = os.path.realpath(expand(cwd)), []
        for team, t in self.data.get("teams", {}).items():
            for folder in t.get("folders", []):
                folder = os.path.realpath(expand(folder))
                if os.path.commonpath([cwd, folder]) == folder:
                    matches.append((len(folder), team))
        best = [team for n, team in matches if n == max(matches)[0]] if matches else []
        self.tied = sorted(set(best)) if len(set(best)) > 1 else []
        return best[0] if len(set(best)) == 1 else None

    def team_by_workspace(self, workspace):
        return next((team for team, t in self.data.get("teams", {}).items()
                     if workspace and t.get("workspace") == workspace), None)

    def refresh(self, conn):
        """Reload on an mtime change. A file that cannot be read keeps the last good structure."""
        at = db.now()
        try:
            mtime = os.stat(self.path).st_mtime_ns
            if mtime != self.mtime:
                with open(self.path, "rb") as f:
                    data = tomllib.load(f)
                check(data)
                self.data, self.mtime = data, mtime
        except (OSError, tomllib.TOMLDecodeError, ValueError) as e:
            with conn:
                db.set_health(conn, "structure", at, "failing", str(e))
            return
        with conn:
            db.set_health(conn, "structure", at)
