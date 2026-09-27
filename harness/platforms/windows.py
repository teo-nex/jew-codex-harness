"""Per-user Windows Task Scheduler adapter, using paths rather than secrets."""

import csv
import io
import json
import os
import runpy
import socket
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET


SERVICE_ID = "JevCodexHarnessRouter"
MARKER = "jev-codex-harness:v1"
PATH_ENV = ("CODEX_HOME", "CODEX_ROUTER_STATE_DIR", "JEV_LADDER_CONFIG",
            "JEV_LADDER_STATE", "JEV_OMNIROUTE_AUTH_FILE", "TYPESAFE_API_KEY_FILE",
            "JEV_DECISION_KEY_FILE")
NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _port_occupied(port):
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _environment(codex_home, state_dir, env):
    provider = env.get("JEV_DECISION_PROVIDER", "typesafe")
    if provider not in ("typesafe", "openrouter"):
        raise ValueError("unsupported Jev decision provider")
    if (provider == "typesafe" and "JEV_DECISION_KEY_FILE" in env
            or provider == "openrouter" and "TYPESAFE_API_KEY_FILE" in env):
        raise ValueError("decision key path does not match provider")
    mode = env.get("JEV_LADDER_MODE", "active")
    if mode not in ("active", "native"):
        raise ValueError("unsupported ladder mode")
    if mode == "native" and any(key in env for key in ("JEV_LADDER_CONFIG", "JEV_LADDER_STATE", "JEV_OMNIROUTE_AUTH_FILE")):
        raise ValueError("native mode must not configure an external provider ladder")
    values = {"CODEX_HOME": str(codex_home),
              "CODEX_ROUTER_STATE_DIR": str(codex_home / "codex-router"),
              "JEV_LISTEN_PORT": str(env.get("JEV_LISTEN_PORT", "4321")),
              "JEV_LADDER_MODE": mode, "JEV_DECISION_PROVIDER": provider}
    if mode == "active":
        values.update(JEV_LADDER_CONFIG=str(state_dir / "ladder-config.json"),
                      JEV_LADDER_STATE=str(state_dir / "ladder-state.json"))
    port = int(values["JEV_LISTEN_PORT"])
    if not 1024 <= port <= 65535:
        raise ValueError("JEV_LISTEN_PORT must be an unprivileged TCP port")
    for key in PATH_ENV:
        if key in env:
            value = str(env[key])
            if not Path(value).is_absolute():
                raise ValueError(f"{key} must be an absolute path")
            if key in ("CODEX_HOME", "CODEX_ROUTER_STATE_DIR"):
                if Path(value).resolve() != Path(values[key]).resolve():
                    raise ValueError(f"{key} is fixed by the selected Codex profile")
                continue
            values[key] = value
    forbidden = set(env) - set(PATH_ENV) - {"JEV_LISTEN_PORT", "JEV_LADDER_MODE", "JEV_DECISION_PROVIDER"}
    if forbidden:
        raise ValueError("unsupported service environment keys: " + ", ".join(sorted(forbidden)))
    return values


def plan_service(repo: Path, codex_home: Path, state_dir: Path, env: dict[str, str]) -> dict:
    repo, codex_home, state_dir = (Path(p).expanduser().resolve() for p in
                                    (repo, codex_home, state_dir))
    values = _environment(codex_home, state_dir, env)
    server = repo / "server/jev_server.py"
    if not server.is_file():
        raise FileNotFoundError(server)
    definition = state_dir / "windows-task.xml"
    if os.name == "nt":
        identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                  capture_output=True, text=True, check=True)
        owner_sid = next((cell.strip().strip('"') for row in csv.reader(
            io.StringIO(identity.stdout)) for cell in row if cell.strip().startswith("S-1-")), None)
        if not owner_sid:
            raise RuntimeError("could not determine current Windows user SID")
    else:
        owner_sid = "S-1-5-21-TEST-USER"
    return {"platform": "windows", "service_id": SERVICE_ID, "marker": MARKER,
            "definition_path": str(definition), "repo": str(repo),
            "command": [sys.executable, str(server)], "env_paths": values,
            "port": int(values["JEV_LISTEN_PORT"]), "state_dir": str(state_dir),
            "wrapper_path": str(state_dir / "run-router.py"), "owner_sid": owner_sid}


def _query_xml():
    return subprocess.run(["schtasks", "/Query", "/TN", SERVICE_ID, "/XML"],
                          capture_output=True, text=True, check=False)


def _owned_xml(xml, plan=None):
    try:
        root = ET.fromstring(xml)
        description = root.find(f".//{{{NS}}}Description")
        if description is None or description.text != MARKER:
            return False
        principal = root.find(f".//{{{NS}}}Principal")
        logon = principal.find(f"{{{NS}}}LogonType") if principal is not None else None
        runlevel = principal.find(f"{{{NS}}}RunLevel") if principal is not None else None
        actions = root.find(f".//{{{NS}}}Exec")
        command = actions.find(f"{{{NS}}}Command") if actions is not None else None
        arguments = actions.find(f"{{{NS}}}Arguments") if actions is not None else None
        working = actions.find(f"{{{NS}}}WorkingDirectory") if actions is not None else None
        # A description alone is forgeable. Require the expected per-user
        # task shape and the exact installer-owned wrapper and working path.
        user_id = principal.find(f"{{{NS}}}UserId") if principal is not None else None
        expected_command = plan["command"][0] if plan else None
        expected_arguments = subprocess.list2cmdline([plan["wrapper_path"]]) if plan else None
        expected_working = plan["repo"] if plan else None
        return (principal is not None and user_id is not None
                and (plan is None or user_id.text == plan["owner_sid"])
                and logon is not None and logon.text == "InteractiveToken"
                and runlevel is not None and runlevel.text == "LeastPrivilege"
                and command is not None and arguments is not None and working is not None
                and command.text and arguments.text and working.text
                and (plan is None or (command.text == expected_command
                                     and arguments.text == expected_arguments
                                     and working.text == expected_working)))
    except ET.ParseError:
        return False


def _active():
    # Task Scheduler's CSV status is localized. The .NET State enum is stable.
    script = ("$t=Get-ScheduledTask -TaskName $env:JEV_TASK_NAME "
              "-ErrorAction SilentlyContinue; "
              "if($t -and $t.State -eq 'Running'){exit 0}else{exit 1}")
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            env={**os.environ, "JEV_TASK_NAME": SERVICE_ID},
                            capture_output=True, check=False)
    return result.returncode == 0


def service_status(plan: dict) -> dict:
    query = _query_xml()
    installed = query.returncode == 0
    return {"installed": installed, "owned": _owned_xml(query.stdout, plan) if installed else False,
            "active": _active() if installed else False,
            "port_occupied": _port_occupied(int(plan["port"])),
            "service_id": SERVICE_ID}


def _render(plan):
    task = ET.Element("Task", {"version": "1.2", "xmlns": NS})
    registration = ET.SubElement(task, "RegistrationInfo")
    ET.SubElement(registration, "Description").text = MARKER
    triggers = ET.SubElement(task, "Triggers")
    ET.SubElement(ET.SubElement(triggers, "LogonTrigger"), "Enabled").text = "true"
    principals = ET.SubElement(task, "Principals")
    principal = ET.SubElement(principals, "Principal", {"id": "Author"})
    ET.SubElement(principal, "UserId").text = plan["owner_sid"]
    ET.SubElement(principal, "LogonType").text = "InteractiveToken"
    ET.SubElement(principal, "RunLevel").text = "LeastPrivilege"
    settings = ET.SubElement(task, "Settings")
    ET.SubElement(settings, "MultipleInstancesPolicy").text = "IgnoreNew"
    ET.SubElement(settings, "ExecutionTimeLimit").text = "PT0S"
    ET.SubElement(settings, "StartWhenAvailable").text = "true"
    actions = ET.SubElement(task, "Actions", {"Context": "Author"})
    action = ET.SubElement(actions, "Exec")
    ET.SubElement(action, "Command").text = plan["command"][0]
    ET.SubElement(action, "Arguments").text = subprocess.list2cmdline([plan["wrapper_path"]])
    ET.SubElement(action, "WorkingDirectory").text = plan["repo"]
    return ET.tostring(task, encoding="unicode", xml_declaration=True)


def _wrapper(plan):
    # JSON is a safe Python string literal subset. This file contains only
    # paths and fixed mode/port values, never key material.
    return ("# " + MARKER + "\n"
            "import os, runpy, sys\n"
            "os.environ.update(" + json.dumps(plan["env_paths"], ensure_ascii=True) + ")\n"
            "sys.path.insert(0, " + json.dumps(str(Path(plan["command"][1]).parent)) + ")\n"
            "runpy.run_path(" + json.dumps(plan["command"][1]) + ", run_name='__main__')\n")


def _owned_local_file(path: Path, plan: dict) -> bool:
    try:
        if not path.is_file() or path.is_symlink():
            return False
        content = path.read_text(encoding="utf-8")
        if path == Path(plan["definition_path"]):
            return _owned_xml(content, plan)
        return content.startswith("# " + MARKER + "\n")
    except (OSError, UnicodeError):
        return False


def _private_state_dir(path: Path, create: bool = False):
    if path.is_symlink():
        raise RuntimeError("router state directory must not be a symlink")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise RuntimeError("router state directory is unavailable")
    # Reset inherited broad access and grant the current user plus Windows
    # service administrators access. Environment variables carry the path so
    # PowerShell never has to parse user-controlled path text as code.
    script = ("$p=$env:JEV_STATE_PATH; $acl=Get-Acl -LiteralPath $p; "
              "$acl.SetAccessRuleProtection($true,$false); "
              "$rules=@($acl.Access); foreach($old in $rules) { [void]$acl.RemoveAccessRuleSpecific($old) }; "
              "$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User; "
              "$inherit=[Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'; "
              "$prop=[Security.AccessControl.PropagationFlags]::None; "
              "$allow=[Security.AccessControl.AccessControlType]::Allow; "
              "foreach($id in @($sid.Value,'S-1-5-18','S-1-5-32-544')) { "
              "$rule=[Security.AccessControl.FileSystemAccessRule]::new($id,'FullControl',$inherit,$prop,$allow); "
              "$acl.SetAccessRule($rule) }; Set-Acl -LiteralPath $p -AclObject $acl")
    env = {**os.environ, "JEV_STATE_PATH": str(path)}
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            env=env, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("could not protect router state directory ACL")


def install_service(plan: dict, dry_run: bool = True) -> dict:
    status = service_status(plan)
    if status["installed"] and not status["owned"]:
        raise FileExistsError("foreign scheduled task " + SERVICE_ID)
    if status["port_occupied"] and not status["active"]:
        raise RuntimeError("loopback listener already occupied")
    if status["active"]:
        return {"action": "already_running", "dry_run": dry_run, **status}
    if not dry_run:
        state_dir = Path(plan["state_dir"])
        wrapper_path = Path(plan["wrapper_path"])
        definition_path = Path(plan["definition_path"])
        for path in (wrapper_path, definition_path):
            if path.exists() or path.is_symlink():
                if not _owned_local_file(path, plan):
                    raise FileExistsError("refusing to overwrite foreign installer file: " + str(path))
        _private_state_dir(state_dir, create=True)
        wrapper_path.write_text(_wrapper(plan), encoding="utf-8")
        definition_path.write_text(_render(plan), encoding="utf-8")
        subprocess.run(["schtasks", "/Create", "/TN", SERVICE_ID, "/XML",
                        plan["definition_path"], "/F"], check=True)
        subprocess.run(["schtasks", "/Run", "/TN", SERVICE_ID], check=True)
    return {"action": "install_and_start", "dry_run": dry_run, **status}


def remove_service(plan: dict, dry_run: bool = True) -> dict:
    status = service_status(plan)
    if status["installed"] and not status["owned"]:
        raise FileExistsError("foreign scheduled task " + SERVICE_ID)
    if not dry_run and status["installed"]:
        for key in ("definition_path", "wrapper_path"):
            path = Path(plan[key])
            if (path.exists() or path.is_symlink()) and not _owned_local_file(path, plan):
                raise FileExistsError("refusing to remove foreign installer file: " + str(path))
        if status["active"]:
            subprocess.run(["schtasks", "/End", "/TN", SERVICE_ID], check=True)
        subprocess.run(["schtasks", "/Delete", "/TN", SERVICE_ID, "/F"], check=True)
        for key in ("definition_path", "wrapper_path"):
            Path(plan[key]).unlink(missing_ok=True)
    return {"action": "remove" if status["installed"] else "absent", "dry_run": dry_run, **status}
