"""Validate a release tag and, optionally, the exact distributions to publish."""
from __future__ import annotations

import argparse
import ast
from email.parser import BytesParser
from pathlib import Path
import re
import tarfile
import tomllib
import zipfile


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def check_release(root: Path, tag: str, dist: Path | None = None) -> tuple[str, str]:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    name, version = project["name"], project["version"]
    module = ast.parse((root / "src/agentproxy/__init__.py").read_text())
    module_version = next((ast.literal_eval(node.value) for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)), None)
    if tag != f"v{version}" or module_version != version:
        raise ValueError(f"tag {tag!r}, project {version!r} and module {module_version!r} must match")
    notes = (root / "RELEASE-NOTES.md").read_text().splitlines()
    if not notes or notes[0].strip() != f"# agent-proxy v{version}":
        raise ValueError("release-note heading must name this version without draft/withdrawal markers")
    if dist is not None:
        # uv build creates this ignored marker; it is never uploaded.
        files = [p for p in dist.iterdir() if p.name != ".gitignore"]
        wheels = [p for p in files if p.name.endswith(".whl")]
        sources = [p for p in files if p.name.endswith(".tar.gz")]
        if len(files) != 2 or len(wheels) != 1 or len(sources) != 1:
            raise ValueError("dist must contain exactly one wheel and one source distribution")
        with zipfile.ZipFile(wheels[0]) as wheel:
            entries = [p for p in wheel.namelist() if p.endswith(".dist-info/METADATA")]
            if len(entries) != 1:
                raise ValueError("wheel must have exactly one package metadata entry")
            records = [(wheels[0].name, wheel.read(entries[0]))]
        with tarfile.open(sources[0], "r:gz") as source:
            entries = [p for p in source.getmembers()
                       if len(Path(p.name).parts) == 2 and p.name.endswith("/PKG-INFO") and p.isfile()]
            if len(entries) != 1:
                raise ValueError("sdist must have exactly one root PKG-INFO entry")
            records.append((sources[0].name, source.extractfile(entries[0]).read()))
        for filename, raw in records:
            metadata = BytesParser().parsebytes(raw)
            if (canonical_name(metadata.get("Name") or "") != canonical_name(name)
                or metadata.get("Version") != version):
                raise ValueError(f"{filename}: metadata must name {name} {version}")
            expected = canonical_name(name).replace("-", "_") + f"-{version}"
            valid_name = (filename.startswith(expected + "-") if filename.endswith(".whl")
                          else filename == expected + ".tar.gz")
            if not valid_name:
                raise ValueError(f"{filename}: artifact filename must match the project name and version")
    return name, version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--dist", type=Path)
    args = parser.parse_args()
    try:
        name, version = check_release(Path.cwd(), args.tag, args.dist)
    except (OSError, ValueError, KeyError, SyntaxError, zipfile.BadZipFile, tarfile.TarError) as exc:
        parser.error(str(exc))
    print(f"Release verified: {name} {version} ({args.tag})")


if __name__ == "__main__":
    main()
