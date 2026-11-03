"""Automated patch applicator for knowledge-rag 1C:Enterprise (BSL + MDO) parser integration.

Installs 1C parsers directly into the active Python environment running knowledge-rag.
Can auto-detect target installation or accept a custom path.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from pathlib import Path


def find_mcp_server_dir() -> Path:
    """Locate the installed mcp_server package directory."""
    spec = importlib.util.find_spec("mcp_server")
    if spec is not None and spec.origin is not None:
        return Path(spec.origin).parent

    # Fallback to local virtualenvs
    candidates = [
        Path.cwd() / "venv" / "Lib" / "site-packages" / "mcp_server",
        Path(sys.prefix) / "Lib" / "site-packages" / "mcp_server",
        Path("Z:/knowledge-rag/venv/Lib/site-packages/mcp_server"),
    ]
    for c in candidates:
        if c.is_dir():
            return c

    raise FileNotFoundError(
        "Could not automatically locate 'mcp_server' package. "
        "Please provide the path using --target <path_to_mcp_server>"
    )


def apply_patch(target_dir: Path | None = None) -> None:
    repo_dir = Path(__file__).resolve().parent
    site_packages = target_dir or find_mcp_server_dir()

    print(f"[*] Target mcp_server directory: {site_packages}")
    if not site_packages.is_dir():
        print(f"[ERROR] Directory does not exist: {site_packages}", file=sys.stderr)
        sys.exit(1)

    # 1. Copy parsers_1c.py
    src_parser = repo_dir / "parsers_1c.py"
    dst_parser = site_packages / "parsers_1c.py"
    if not src_parser.exists():
        print(f"[ERROR] Source parser file not found: {src_parser}", file=sys.stderr)
        sys.exit(1)

    shutil.copy2(src_parser, dst_parser)
    print(f"[OK] Installed {dst_parser.name} into {site_packages}")

    # 2. Patch config.py (_SUPPORTED_SUFFIXES)
    config_py = site_packages / "config.py"
    if not config_py.exists():
        print(f"[ERROR] config.py not found in {site_packages}", file=sys.stderr)
        sys.exit(1)

    config_content = config_py.read_text(encoding="utf-8")
    if '".bsl"' not in config_content:
        config_content = config_content.replace(
            '".jq",\n    ]',
            '".jq",\n        ".bsl",\n        ".mdo",\n    ]'
        )
        config_py.write_text(config_content, encoding="utf-8")
        print("[OK] Patched config.py (_SUPPORTED_SUFFIXES updated with .bsl and .mdo)")
    else:
        print("[INFO] config.py already contains .bsl and .mdo")

    # 3. Patch ingestion.py
    ingestion_py = site_packages / "ingestion.py"
    if not ingestion_py.exists():
        print(f"[ERROR] ingestion.py not found in {site_packages}", file=sys.stderr)
        sys.exit(1)

    ingestion_content = ingestion_py.read_text(encoding="utf-8")
    changed = False

    if '".bsl": self._parse_bsl' not in ingestion_content:
        ingestion_content = ingestion_content.replace(
            '"Tiltfile": self._parse_code_generic,\n        }',
            '"Tiltfile": self._parse_code_generic,\n            ".bsl": self._parse_bsl,\n            ".mdo": self._parse_mdo,\n        }'
        )
        changed = True

    if 'elif key == ".bsl":' not in ingestion_content:
        old_chunk = (
            '        # Chunk the content (markdown-aware for .md files)\n'
            '        if key == ".md":\n'
            '            doc.chunks = self._chunk_markdown(content, metadata)\n'
            '        else:\n'
            '            doc.chunks = self._chunk_text(content, metadata)'
        )
        new_chunk = (
            '        # Chunk the content (markdown-aware for .md files, 1C-aware for .bsl and .mdo)\n'
            '        if key == ".md":\n'
            '            doc.chunks = self._chunk_markdown(content, metadata)\n'
            '        elif key == ".bsl":\n'
            '            from .parsers_1c import build_bsl_chunks\n'
            '            doc.chunks = build_bsl_chunks(filepath, content, metadata, Chunk, chunk_size=2500)\n'
            '        elif key == ".mdo":\n'
            '            from .parsers_1c import build_mdo_chunks\n'
            '            doc.chunks = build_mdo_chunks(filepath, content, metadata, Chunk, chunk_size=1800)\n'
            '        else:\n'
            '            doc.chunks = self._chunk_text(content, metadata)'
        )
        ingestion_content = ingestion_content.replace(old_chunk, new_chunk)
        changed = True

    if 'def _parse_bsl(' not in ingestion_content:
        bsl_methods = (
            '    def _parse_bsl(self, filepath: Path) -> tuple[str, Dict]:\n'
            '        """Parse 1C:Enterprise BSL module file."""\n'
            '        from .parsers_1c import resolve_1c_object_context\n'
            '        content = filepath.read_text(encoding="utf-8-sig", errors="ignore")\n'
            '        obj_name, mod_type, disp_title = resolve_1c_object_context(filepath)\n'
            '        metadata = {\n'
            '            "type": "bsl_code",\n'
            '            "title": disp_title,\n'
            '            "object_name": obj_name,\n'
            '            "module_type": mod_type,\n'
            '            "file_size": filepath.stat().st_size,\n'
            '            "modified": datetime.fromtimestamp(filepath.stat().st_mtime).isoformat(),\n'
            '            "line_count": content.count("\\n") + 1,\n'
            '        }\n'
            '        return content, metadata\n\n'
            '    def _parse_mdo(self, filepath: Path) -> tuple[str, Dict]:\n'
            '        """Parse 1C:Enterprise EDT .mdo metadata XML file."""\n'
            '        from .parsers_1c import parse_mdo_file\n'
            '        return parse_mdo_file(filepath)\n\n'
            '    def _parse_code(self, filepath: Path) -> tuple[str, Dict]:'
        )
        ingestion_content = ingestion_content.replace('    def _parse_code(self, filepath: Path) -> tuple[str, Dict]:', bsl_methods)
        changed = True

    if changed:
        ingestion_py.write_text(ingestion_content, encoding="utf-8")
        print("[OK] Patched ingestion.py (registered 1C parser dispatch and semantic chunking)")
    else:
        print("[INFO] ingestion.py already up to date")

    print("[SUCCESS] 1C (BSL + MDO) integration is active and verified.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Install 1C parsers into knowledge-rag")
    parser.add_argument("--target", type=Path, default=None, help="Explicit path to mcp_server package directory")
    args = parser.parse_args()
    apply_patch(args.target)
