"""Per-run metadata: launch arguments, deployment artifact, controllers, plugins, code version, host."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import yaml

from . import __version__
from .procfs import ProcFS, format_cpu_list


def launch_args(cmdline: list[str]) -> dict:
    """`name:=value` arguments of a `ros2 launch` command line."""
    args = {}
    for part in cmdline:
        key, sep, value = part.partition(":=")
        if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            args[key] = value
    return args


def ros_overrides(cmdline: list[str]) -> dict:
    """`-p name:=value` parameter overrides on a ROS node command line."""
    overrides = {}
    for index, part in enumerate(cmdline):
        if part in ("-p", "--param") and index + 1 < len(cmdline):
            key, _, value = cmdline[index + 1].partition(":=")
            if not key.startswith("robot_description"):
                overrides[key] = value[:200]
    return overrides


def params_files(cmdline: list[str]) -> list[str]:
    return [cmdline[i + 1] for i, part in enumerate(cmdline) if part == "--params-file" and i + 1 < len(cmdline)]


def sha256_file(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None


def artifact_info(directory: Path) -> dict:
    info: dict = {"dir": str(directory)}
    for name in ("artifact.yaml", "controllers.yaml", "robot_description.xml", "lifecycle.yaml", "controller_order.yaml"):
        digest = sha256_file(directory / name)
        if digest:
            info.setdefault("sha256", {})[name] = digest
    try:
        artifact = yaml.safe_load((directory / "artifact.yaml").read_text())
        if isinstance(artifact, dict):
            info["selection"] = {k: v for k, v in artifact.items() if not isinstance(v, (dict, list))}
            if isinstance(artifact.get("selection"), dict):
                info["selection"].update(artifact["selection"])
    except (FileNotFoundError, yaml.YAMLError):
        pass
    try:
        controllers = yaml.safe_load((directory / "controllers.yaml").read_text()) or {}
        manager = (controllers.get("controller_manager") or {}).get("ros__parameters", {})
        info["update_rate"] = manager.get("update_rate")
        info["controllers"] = {
            name: (value or {}).get("type") if isinstance(value, dict) else None
            for name, value in manager.items()
            if isinstance(value, dict) and "type" in value
        }
        info["controller_parameters"] = {
            name: body.get("ros__parameters", {})
            for name, body in controllers.items()
            if name != "controller_manager" and isinstance(body, dict)
        }
    except (FileNotFoundError, yaml.YAMLError, AttributeError):
        pass
    try:
        robot = ET.parse(directory / "robot_description.xml").getroot()
        info["hardware"] = [
            {
                "name": system.get("name"),
                "plugin": (system.findtext("hardware/plugin") or "").strip(),
                "joints": len(system.findall("joint")),
            }
            for system in robot.findall("ros2_control")
        ]
    except (FileNotFoundError, ET.ParseError):
        pass
    return info


def git_info(repo: str) -> dict:
    path = Path(repo).expanduser()
    info: dict = {"path": str(path)}

    def git(*args: str) -> Optional[str]:
        try:
            completed = subprocess.run(
                ["git", "-C", str(path), *args], capture_output=True, text=True, timeout=20, check=False
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        return completed.stdout if completed.returncode == 0 else None

    head = git("rev-parse", "HEAD")
    if head is None:
        info["error"] = "not a git repository"
        return info
    info["head"] = head.strip()
    info["branch"] = (git("rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    status = git("status", "--porcelain", "--untracked-files=no") or ""
    info["dirty_files"] = len([line for line in status.splitlines() if line.strip()])
    diff = git("diff", "HEAD") or ""
    info["diff_sha256"] = hashlib.sha256(diff.encode()).hexdigest() if diff else None
    return info


def host_info(proc: ProcFS) -> dict:
    uname = platform.uname()
    return {
        "hostname": uname.node,
        "kernel": uname.release,
        "nproc": os.cpu_count(),
        "isolated_cpus": format_cpu_list(proc.isolated_cpus()),
        "clk_tck": proc.clk_tck,
        "runtime_log_version": __version__,
    }


def hardware_kind(artifact: dict, profile_run: dict, args: dict) -> tuple[bool, bool]:
    """(mock, uses_ethercat) from the artifact's plugins, falling back to the launch argument."""
    plugins = [item.get("plugin", "") for item in artifact.get("hardware", [])]
    markers = profile_run.get("hardware_plugins", {})
    if plugins:
        ethercat = any(markers.get("ethercat", "EthercatDriver") in plugin for plugin in plugins)
        mock = all(markers.get("mock", "mock_components") in plugin for plugin in plugins)
        return mock, ethercat
    value = args.get(profile_run.get("mock_arg", "use_mock_hardware"))
    if value is None:
        mock = bool(profile_run.get("mock_default", True))
    else:
        mock = value.lower() != "false"
    return mock, not mock


def thread_layout(proc: ProcFS, pid: int) -> list[dict]:
    rows = []
    for tid in proc.threads(pid):
        stat = proc.stat(pid, tid)
        if stat is None:
            continue
        rows.append(
            {
                "tid": tid,
                "name": stat["comm"],
                "policy": stat["policy"],
                "rt_priority": stat["rt_priority"],
                "cpu": stat["processor"],
                "affinity": proc.affinity(tid),
            }
        )
    return rows
