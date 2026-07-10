#!/usr/bin/env python3
"""统一升级本地 Codex 插件版本，并可选地重新安装到 Codex。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
PYPROJECT_PATH = PLUGIN_ROOT / "pyproject.toml"
PACKAGE_VERSION_PATH = PLUGIN_ROOT / "agents_workflow" / "__init__.py"
WORKFLOW_PATH = PLUGIN_ROOT / "workflow" / "codex-multi-agents-loop.toml"
SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
PYPROJECT_VERSION_PATTERN = re.compile(r'^(version\s*=\s*)"[^"]+"$', re.MULTILINE)
PACKAGE_VERSION_PATTERN = re.compile(r'^__version__\s*=\s*"[^"]+"$', re.MULTILINE)
WORKFLOW_VERSION_PATTERN = re.compile(r'^(version\s*=\s*)"[^"]+"$', re.MULTILINE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="升级 codex-multi-agents-loop 的版本并同步所有版本来源。")
    parser.add_argument(
        "version",
        nargs="?",
        default="patch",
        help="版本升级类型：patch、minor、major，或明确版本号（例如 0.1.0）。默认 patch。",
    )
    parser.add_argument("--dry-run", action="store_true", help="只显示将要写入的版本，不修改文件。")
    parser.add_argument("--no-cachebuster", action="store_true", help="不添加 +codex.<UTC 时间戳> 缓存后缀。")
    parser.add_argument("--install", action="store_true", help="写入版本后执行 Codex 的正常插件安装/刷新命令。")
    parser.add_argument("--marketplace", help="执行 --install 时使用的已配置 marketplace 名称。")
    args = parser.parse_args()
    if args.dry_run and args.install:
        parser.error("--dry-run 不能与 --install 同时使用")
    if args.install and not args.marketplace:
        parser.error("--install 必须同时提供 --marketplace <名称>")
    return args


def _base_version(version: str) -> str:
    """版本比较只使用 + 前的语义版本，缓存后缀由本工具统一重建。"""
    base = version.split("+", 1)[0]
    if not SEMVER_PATTERN.fullmatch(base):
        raise ValueError(f"版本必须是 x.y.z 格式，当前值为：{version}")
    return base


def next_base_version(current: str, requested: str) -> str:
    current_base = _base_version(current)
    if requested not in {"patch", "minor", "major"}:
        return _base_version(requested)

    major, minor, patch = (int(part) for part in current_base.split("."))
    if requested == "major":
        return f"{major + 1}.0.0"
    if requested == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def with_cachebuster(base_version: str, enabled: bool) -> str:
    if not enabled:
        return base_version
    # Codex 只保留一个缓存后缀，时间戳确保同一语义版本的本地迭代也能被重新读取。
    cachebuster = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{base_version}+codex.{cachebuster}"


def _read_json_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} 必须是 JSON 对象")
    return payload


def _read_manifest_version() -> tuple[str, str]:
    manifest = _read_json_object(MANIFEST_PATH)
    name = manifest.get("name")
    version = manifest.get("version")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{MANIFEST_PATH} 缺少插件名称")
    if not isinstance(version, str) or not version:
        raise ValueError(f"{MANIFEST_PATH} 缺少版本号")
    _base_version(version)
    return name, version


def _read_toml_version(path: Path, pattern: re.Pattern[str]) -> str:
    match = pattern.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"{path} 缺少 version 字段")
    return match.group(0).split('"', 2)[1]


def _read_package_version() -> str:
    match = PACKAGE_VERSION_PATTERN.search(PACKAGE_VERSION_PATH.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"{PACKAGE_VERSION_PATH} 缺少 __version__")
    return match.group(0).split('"', 2)[1]


def _ensure_version_sources_are_synced(manifest_version: str) -> None:
    versions = {
        "pyproject.toml": _read_toml_version(PYPROJECT_PATH, PYPROJECT_VERSION_PATTERN),
        "agents_workflow/__init__.py": _read_package_version(),
        "workflow/codex-multi-agents-loop.toml": _read_toml_version(WORKFLOW_PATH, WORKFLOW_VERSION_PATTERN),
    }
    mismatches = [f"{path}={version}" for path, version in versions.items() if version != manifest_version]
    if mismatches:
        details = "，".join(mismatches)
        raise ValueError(f"版本来源不一致，请先修复：plugin.json={manifest_version}；{details}")


def _replace_version(path: Path, pattern: re.Pattern[str], next_version: str, *, prefix_group: bool = True) -> None:
    content = path.read_text(encoding="utf-8")
    replacement = rf'\g<1>"{next_version}"' if prefix_group else f'__version__ = "{next_version}"'
    updated, count = pattern.subn(replacement, content, count=1)
    if count != 1:
        raise ValueError(f"无法更新 {path} 的版本字段")
    path.write_text(updated, encoding="utf-8")


def _write_versions(next_version: str) -> None:
    manifest = _read_json_object(MANIFEST_PATH)
    manifest["version"] = next_version
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    _replace_version(PYPROJECT_PATH, PYPROJECT_VERSION_PATTERN, next_version)
    _replace_version(PACKAGE_VERSION_PATH, PACKAGE_VERSION_PATTERN, next_version, prefix_group=False)
    _replace_version(WORKFLOW_PATH, WORKFLOW_VERSION_PATTERN, next_version)


def _install(plugin_name: str, marketplace_name: str) -> None:
    command = ["codex", "plugin", "add", f"{plugin_name}@{marketplace_name}"]
    print(f"执行安装：{' '.join(command)}")
    subprocess.run(command, check=True)


def main() -> None:
    args = parse_args()
    plugin_name, current_version = _read_manifest_version()
    _ensure_version_sources_are_synced(current_version)
    base_version = next_base_version(current_version, args.version)
    next_version = with_cachebuster(base_version, enabled=not args.no_cachebuster)

    print(f"{plugin_name}: {current_version} -> {next_version}")
    if args.dry_run:
        return

    _write_versions(next_version)
    _ensure_version_sources_are_synced(next_version)
    if args.install:
        _install(plugin_name, args.marketplace)
    else:
        print("版本已同步。若需刷新安装，请使用 --install --marketplace <名称>。")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"升级失败：{error}", file=sys.stderr)
        raise SystemExit(1) from error
