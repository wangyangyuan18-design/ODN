#!/usr/bin/env python3
"""Static production-build validation for ODN Tools Pro.

The CI job deliberately does not import QGIS itself. Instead it validates the
plugin's file/interface contract that can be checked without a QGIS runtime:
- metadata.txt exists and is structurally valid
- metadata icon points to an existing file
- __init__.py exposes classFactory
- all relative Python imports resolve to files/packages in the repository
- explicitly referenced local files in common path APIs exist
- no duplicate canonical Link Design entry point is accidentally introduced
"""

from __future__ import annotations

import ast
import configparser
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = [
    Path("metadata.txt"),
    Path("__init__.py"),
    Path("cable_offset_core.py"),
    Path("cable_offset_layout.py"),
    Path("link_design.py"),
    Path("link_design_core.py"),
    Path("odn_project.py"),
    Path("odn_project_context.py"),
    Path("odn_project_manager.py"),
    Path("odn_project_config.py"),
    Path("odn_project_validation.py"),
    Path("odn_project_integration.py"),
    Path("odn_project_routing.py"),
    Path("odn_project_workspace.py"),
    Path("link_design_features.py"),
    Path("fat_return.py"),
    Path("plugin_undo.py"),
    Path("icons/odn_tools_pro.svg"),
]

EXCLUDED_PARTS = {".git", ".github", "__pycache__", "dist"}


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def resolve_module(base_file: Path, module: str, level: int) -> Path | None:
    if level <= 0:
        return None
    package_dir = base_file.parent
    for _ in range(level - 1):
        package_dir = package_dir.parent
    parts = [p for p in module.split(".") if p]
    candidate = package_dir.joinpath(*parts)
    py_file = candidate.with_suffix(".py")
    if py_file.is_file():
        return py_file
    init_file = candidate / "__init__.py"
    if init_file.is_file():
        return init_file
    return None


def validate_python_imports() -> None:
    for path in ROOT.rglob("*.py"):
        if any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            fail(f"syntax error in {path.relative_to(ROOT)}: {exc}")

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                target = resolve_module(path, node.module or "", node.level)
                if target is None:
                    module_name = "." * node.level + (node.module or "")
                    fail(
                        f"broken relative import in {path.relative_to(ROOT)}: "
                        f"{module_name}"
                    )


def validate_referenced_paths() -> None:
    # Covers common resource/path calls without trying to execute QGIS code.
    patterns = [
        re.compile(r"[\"'](icons/[^\"']+)[\"']"),
        re.compile(r"[\"'](tools/[^\"']+)[\"']"),
    ]
    for path in ROOT.rglob("*.py"):
        if any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            for match in pattern.finditer(text):
                target = ROOT / match.group(1)
                if not target.is_file():
                    fail(
                        f"referenced local file does not exist: "
                        f"{match.group(1)} (from {path.relative_to(ROOT)})"
                    )


def validate_metadata() -> None:
    path = ROOT / "metadata.txt"
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read(path, encoding="utf-8")
    if "general" not in parser:
        fail("metadata.txt has no [general] section")

    section = parser["general"]
    for key in ("name", "version", "qgisMinimumVersion", "icon"):
        if not section.get(key, "").strip():
            fail(f"metadata.txt missing required field: {key}")

    icon = Path(section["icon"].strip())
    if icon.is_absolute() or ".." in icon.parts:
        fail(f"metadata icon path is unsafe: {icon}")
    if not (ROOT / icon).is_file():
        fail(f"metadata icon does not exist: {icon}")


def validate_entrypoint() -> None:
    path = ROOT / "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    has_factory = any(
        isinstance(node, ast.FunctionDef) and node.name == "classFactory"
        for node in tree.body
    )
    if not has_factory:
        fail("__init__.py does not define classFactory")

    text = path.read_text(encoding="utf-8")
    for required in (
        "link_design import LinkDesignDock",
        "PoleTraceDialog",
        "OverlengthPoleDialog",
        "OdnProjectWizard",
    ):
        if required not in text:
            fail(f"__init__.py missing expected runtime interface: {required}")


def validate_canonical_runtime() -> None:
    link_design = (ROOT / "link_design.py").read_text(encoding="utf-8")
    core = (ROOT / "link_design_core.py").read_text(encoding="utf-8")
    offset = (ROOT / "cable_offset_core.py").read_text(encoding="utf-8")

    if "from .link_design_core import" not in link_design:
        fail("link_design.py is not wired to link_design_core.py")
    if "from . import cable_offset_core as offset_core" not in core:
        fail("link_design_core.py is not wired to cable_offset_core.py")
    if "Authoritative ODN cable offset engine" not in offset:
        fail("cable_offset_core.py does not appear to be the authoritative offset engine")


def validate_required_files() -> None:
    for relative in REQUIRED_FILES:
        if not (ROOT / relative).is_file():
            fail(f"required plugin file is missing: {relative}")


def main() -> None:
    validate_required_files()
    validate_metadata()
    validate_entrypoint()
    validate_python_imports()
    validate_referenced_paths()
    validate_canonical_runtime()
    print("ODN Tools Pro production validation: PASS")


if __name__ == "__main__":
    main()
