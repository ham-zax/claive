"""Team launch layers and read-only team/alias views."""
import json
from pathlib import Path
import sys

from claivelib import aliases, config
from claivelib.prompts import read_layer_file


ACTIVE_ALIAS_STATUSES = {"starting", "running", "cancelling", "idle"}


def _team_data(config_data=None):
    return (config_data if config_data is not None else config.load()).get("teams", {})


def _file_layer(path, team, kind):
    if path is None:
        return None
    _resolved, text, _data = read_layer_file(path, f"team {team} {kind}")
    heading = "team collaboration" if kind == "collaboration" else "project"
    return f"[{heading}: {Path(path).name}]\n{text}"


def prepare_launch(team_name, member_name, role, project_file=None, config_data=None):
    """Resolve team launch options, validate its files, and refuse active aliases."""
    if bool(team_name) != bool(member_name):
        raise ValueError("--team and --member go together")
    if project_file is not None and team_name is None:
        raise ValueError("--project-file needs --team")
    if team_name is None:
        return None
    if role is not None:
        raise ValueError("--team sets the role from the member; do not pass --role")

    teams = _team_data(config_data)
    team = teams.get(team_name)
    if team is None:
        raise ValueError(f"unknown team: {team_name}")
    members = team["members"]
    if member_name not in members:
        raise ValueError(f"team {team_name} has no member {member_name}; members: {', '.join(sorted(members))}")

    role_name = members[member_name]
    alias_name = f"{team_name}-{member_name}"
    binding = aliases.binding(alias_name)
    if binding and binding["status"] in ACTIVE_ALIAS_STATUSES:
        raise ValueError(f"@{alias_name} is bound to active worker {binding['id']}; close it or run claive alias remove {alias_name}")

    header = [f"[team: {team_name}]",
              f"You are member {member_name} (role {role_name}), alias @{alias_name}.",
              "Members:"]
    for name, member_role in members.items():
        header.append(f"- {name} (role {member_role}): @{team_name}-{name}")
    header.append("The parent coordinates the team. Report to the parent in your final answer.")

    collaboration = _file_layer(team.get("collaboration"), team_name, "collaboration")
    project_path = project_file if project_file is not None else team.get("project")
    project = _file_layer(project_path, team_name, "project")
    return dict(role=role_name, header="\n".join(header),
                collaboration=collaboration, project=project,
                state=dict(name=team_name, member=member_name, role=role_name), alias=alias_name)


def bind_worker(team_state, worker_id):
    """Bind a successfully created team worker and report the alias on stderr."""
    return aliases.update(f"{team_state['name']}-{team_state['member']}", worker_id,
                          replace=True, output=sys.stderr)


def listing(json_output=False):
    """Print a concise team index without touching worker state."""
    rows = [dict(name=name, member_count=len(team["members"]),
                 collaboration=team.get("collaboration"))
            for name, team in sorted(_team_data().items())]
    if json_output:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print("No teams.")
    else:
        for row in rows:
            print(f"{row['name']} | members: {row['member_count']} | collaboration: {row['collaboration'] or 'none'}")
    return 0


def show(name, json_output=False):
    """Print one team's config and current aliases using read-only filesystem reads."""
    teams = _team_data()
    team = teams.get(name)
    if team is None:
        raise ValueError(f"unknown team: {name}")
    members = []
    for member, role in team["members"].items():
        alias_name = f"{name}-{member}"
        binding = aliases.binding(alias_name)
        members.append(dict(name=member, role=role, alias=f"@{alias_name}",
                            worker_id=binding["id"] if binding else None,
                            status=binding["status"] if binding else "none"))
    result = dict(name=name, collaboration=team.get("collaboration"), project=team.get("project"),
                  members=members)
    if json_output:
        print(json.dumps(result, indent=2))
    else:
        print(f"Team {name}")
        print(f"Collaboration: {result['collaboration'] or 'none'}")
        print(f"Project: {result['project'] or 'none'}")
        print("Members:")
        for member in members:
            worker = (f"{member['worker_id']} ({member['status']})" if member["worker_id"]
                      else "none")
            print(f"- {member['name']} | role {member['role']} | {member['alias']} | worker {worker}")
    return 0
