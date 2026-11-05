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
    config_changed = False
    if '".bsl"' not in config_content:
        config_content = config_content.replace(
            '".jq",\n    ]',
            '".jq",\n        ".bsl",\n        ".mdo",\n    ]'
        )
        config_changed = True
        print("[OK] Patched config.py (_SUPPORTED_SUFFIXES updated with .bsl and .mdo)")

    if 'include_patterns: List[str]' not in config_content:
        config_content = config_content.replace(
            '    # Exclude patterns for directory traversal\n    exclude_patterns: List[str]',
            '    # Include patterns for directory traversal (empty = include all supported)\n    include_patterns: List[str] = field(default_factory=lambda: _get("documents", "include_patterns", []))\n\n    # Exclude patterns for directory traversal\n    exclude_patterns: List[str]'
        )
        config_changed = True
        print("[OK] Patched config.py (added include_patterns field)")

    if config_changed:
        config_py.write_text(config_content, encoding="utf-8")

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

    if 'def _should_include(' not in ingestion_content:
        should_inc_code = (
            '    @staticmethod\n'
            '    def _should_include(path: Path, base_dir: Path, patterns: List[str]) -> bool:\n'
            '        """Check if a path matches any include pattern."""\n'
            '        if not patterns:\n'
            '            return True\n'
            '        try:\n'
            '            rel = path.relative_to(base_dir)\n'
            '        except ValueError:\n'
            '            rel = path\n'
            '        rel_str = str(rel).replace("\\\\", "/")\n'
            '        rel_str_lower = rel_str.lower()\n'
            '        for pattern in patterns:\n'
            '            pat_lower = pattern.lower()\n'
            '            if fnmatch.fnmatch(rel_str_lower, pat_lower) or fnmatch.fnmatch(rel_str_lower, f"**/{pat_lower.lstrip(\'/\')}"):\n'
            '                return True\n'
            '            pat_prefix = pat_lower.split("/**")[0].split("/*")[0].strip("/")\n'
            '            for part in rel.parts:\n'
            '                if fnmatch.fnmatch(part.lower(), pat_prefix):\n'
            '                    return True\n'
            '        return False\n\n'
            '    @staticmethod\n'
            '    def _should_exclude('
        )
        ingestion_content = ingestion_content.replace('    @staticmethod\n    def _should_exclude(', should_inc_code)
        changed = True

    if 'include = getattr(config, "include_patterns", None)' not in ingestion_content:
        ingestion_content = ingestion_content.replace(
            '        exclude = config.exclude_patterns\n\n        for root, dirs, files in walk_document_paths(directory):',
            '        exclude = config.exclude_patterns\n        include = getattr(config, "include_patterns", None)\n\n        for root, dirs, files in walk_document_paths(directory):'
        )
        ingestion_content = ingestion_content.replace(
            '                if exclude and self._should_exclude(filepath, directory, exclude):\n                    continue\n                if not is_path_within(directory, filepath):',
            '                if exclude and self._should_exclude(filepath, directory, exclude):\n                    continue\n                if include and not self._should_include(filepath, directory, include):\n                    continue\n                if not is_path_within(directory, filepath):'
        )
        changed = True

    if 'if path_pattern.lower() in path_str:' not in ingestion_content:
        ingestion_content = ingestion_content.replace(
            '        for path_pattern, category in sorted(config.category_mappings.items(), key=lambda x: len(x[0]), reverse=True):\n            if path_pattern in path_str:',
            '        for path_pattern, category in sorted(config.category_mappings.items(), key=lambda x: len(x[0]), reverse=True):\n            if path_pattern.lower() in path_str:'
        )
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

    # 4. Patch server.py (0-byte file handling & BM25 memory optimization)
    server_py = site_packages / "server.py"
    if server_py.exists():
        server_content = server_py.read_text(encoding="utf-8")
        server_changed = False

        # 4a. 0-byte stub handling: treat doc is None as skipped instead of fatal error
        old_empty_doc = (
            '                doc = self.parser.parse_file(doc)\n'
            '                if doc is None:\n'
            '                    raise ValueError("Document has no indexable content")'
        )
        new_empty_doc = (
            '                doc = self.parser.parse_file(doc)\n'
            '                if doc is None:\n'
            '                    stats["skipped"] += 1\n'
            '                    return'
        )
        if old_empty_doc in server_content:
            server_content = server_content.replace(old_empty_doc, new_empty_doc)
            server_changed = True
            print("[OK] Patched server.py (0-byte empty files handled gracefully as skipped)")

        # 4b. BM25Index memory release after snapshot
        old_snapshot = (
            '        self._snapshot = _BM25Snapshot(tuple(self.corpus_ids), inverted, idf, doc_lengths, avgdl)\n'
            '\n'
            '    def search('
        )
        new_snapshot = (
            '        self._snapshot = _BM25Snapshot(tuple(self.corpus_ids), inverted, idf, doc_lengths, avgdl)\n'
            '\n'
            '        # Free raw text and token lists to keep RAM footprint minimal (< 2 GB instead of 36 GB)\n'
            '        self._tokenized_corpus.clear()\n'
            '        self.corpus.clear()\n'
            '        self._token_pool.clear()\n'
            '\n'
            '    def search('
        )
        if old_snapshot in server_content:
            server_content = server_content.replace(old_snapshot, new_snapshot)
            server_changed = True
            print("[OK] Patched server.py (BM25Index memory release enabled)")

        # 4c. BM25Index.__len__ return self._corpus_size
        old_len = (
            '    def __len__(self) -> int:\n'
            '        return len(self.corpus)'
        )
        new_len = (
            '    def __len__(self) -> int:\n'
            '        return self._corpus_size'
        )
        if old_len in server_content:
            server_content = server_content.replace(old_len, new_len)
            server_changed = True
            print("[OK] Patched server.py (BM25Index.__len__ uses _corpus_size)")

        # 4d. Post-indexing GPU VRAM release (unload_embedding_models)
        old_index_finally = (
            '        try:\n'
            '            return self._index_all_impl(force, resume_state=resume_state)\n'
            '        finally:\n'
            '            self._index_lock.release()'
        )
        new_index_finally = (
            '        try:\n'
            '            return self._index_all_impl(force, resume_state=resume_state)\n'
            '        finally:\n'
            '            self._index_lock.release()\n'
            '            self.unload_embedding_models()'
        )
        if 'def unload_embedding_models(self)' not in server_content:
            unload_method = (
                '    def unload_embedding_models(self) -> None:\n'
                '        """Unload embedding and reranker ONNX sessions after batch indexing.\n\n'
                '        Destroys the InferenceSession C++ objects, triggering cudaFree for all\n'
                '        cached BFCArena memory allocations and returning 100% of GPU VRAM to the OS.\n'
                '        Subsequent queries reload only what is needed for single queries (~1.8 GB).\n'
                '        """\n'
                '        try:\n'
                '            if hasattr(self, "embed_fn") and hasattr(self.embed_fn, "_load_lock"):\n'
                '                with self.embed_fn._load_lock:\n'
                '                    self.embed_fn._model = None\n'
                '            if hasattr(self, "reranker") and hasattr(self.reranker, "_load_lock"):\n'
                '                with self.reranker._load_lock:\n'
                '                    self.reranker._model = None\n'
                '            import gc\n\n'
                '            gc.collect()\n'
                '            print("[INFO] Embedding and reranker models unloaded, GPU memory released", file=sys.stderr)\n'
                '        except Exception as e:\n'
                '            print(f"[WARN] Failed to unload embedding models: {e}", file=sys.stderr)\n\n'
                '    def index_all('
            )
            server_content = server_content.replace('    def index_all(', unload_method)
            server_changed = True
            if old_index_finally in server_content:
                server_content = server_content.replace(old_index_finally, new_index_finally)
            print("[OK] Patched server.py (added unload_embedding_models and post-index VRAM release)")

        if server_changed:
            server_py.write_text(server_content, encoding="utf-8")
        else:
            print("[INFO] server.py already up to date")

    print("[SUCCESS] 1C (BSL + MDO) integration is active and verified.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Install 1C parsers into knowledge-rag")
    parser.add_argument("--target", type=Path, default=None, help="Explicit path to mcp_server package directory")
    args = parser.parse_args()
    apply_patch(args.target)
