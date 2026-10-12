"""Append the recommended plugin actions without rewriting user configuration."""
import os
import stat
import subprocess
import tempfile
import tomllib
from pathlib import Path

from . import PLUGIN_ID
from .store import Conflict

SHORTCUTS = {
    "prefix+shift+o": "status",
}


def install_shortcuts(herdr):
    configured = herdr.env.get("HERDR_CONFIG_PATH")
    if configured is None:
        try:
            result = subprocess.run([herdr.binary, "--help"], env=herdr.env,
                                    capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("Herdr config path lookup timed out") from error
        if result.returncode:
            raise Conflict("Herdr config path lookup failed: " + (result.stdout + result.stderr).strip())
        configured = next((line.removeprefix("Config: ") for line in result.stdout.splitlines()
                           if line.startswith("Config: ")), None)
        if not configured:
            raise Conflict("Herdr did not report its config path; set HERDR_CONFIG_PATH explicitly")
    path = Path(configured).expanduser()
    path = path.resolve()
    original = path.read_bytes() if path.exists() else None
    content = original.decode("utf-8") if original is not None else ""
    config = tomllib.loads(content)
    keys = config.get("keys", {})
    if not isinstance(keys, dict):
        raise Conflict("keys must be a TOML table")
    commands = keys.get("command", [])
    if not isinstance(commands, list) or any(not isinstance(item, dict) for item in commands):
        raise Conflict("keys.command must contain TOML command tables")
    missing = []
    for key, action in SHORTCUTS.items():
        matches = [item for item in commands if item.get("key") == key]
        if matches and (len(matches) != 1 or matches[0].get("type") != "plugin_action"
                        or matches[0].get("command") != f"{PLUGIN_ID}.{action}"):
            raise Conflict(f"shortcut {key} is occupied; configuration was not changed")
        if not matches:
            missing.append((key, action))
    candidate = content
    for key, action in missing:
        candidate += (f'\n[[keys.command]]\nkey = "{key}"\ntype = "plugin_action"\n'
                      f'command = "{PLUGIN_ID}.{action}"\n')
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".orchestrator-", delete=False) as file:
        temporary = Path(file.name)
        file.write(candidate.encode("utf-8"))
        file.flush()
        os.fsync(file.fileno())
    try:
        env = dict(herdr.env, HERDR_CONFIG_PATH=str(temporary))
        try:
            result = subprocess.run([herdr.binary, "config", "check"], env=env,
                                    capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("Herdr config check timed out; configuration was not changed") from error
        if result.returncode:
            raise Conflict("shortcut candidate rejected; configuration was not changed: "
                           + (result.stdout + result.stderr).strip())
        if not missing:
            return {"config": str(path), "installed": [], "already_installed": True,
                    "check_output": result.stdout}
        if (path.read_bytes() if path.exists() else None) != original:
            raise Conflict("configuration changed during validation; retry after reviewing it")
        backup = path.with_name(path.name + ".herdr-orchestrator.bak")
        if original is not None:
            mode = stat.S_IMODE(path.stat().st_mode)
            os.chmod(temporary, mode)
            try:
                descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as file:
                    file.write(original)
                    file.flush()
                    os.fsync(file.fileno())
            except FileExistsError:
                pass
            os.chmod(backup, 0o600)
        os.replace(temporary, path)
        return {"config": str(path), "installed": [key for key, _ in missing],
                "backup": str(backup) if original is not None else None,
                "check_output": result.stdout, "reload_required": True}
    finally:
        temporary.unlink(missing_ok=True)
