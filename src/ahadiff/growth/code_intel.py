"""Index an exact captured worktree in an isolated CodeGraph workspace."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from .git_snapshot import CapturedSnapshot, capture_worktree, git, repository_root

MAX_INDEX_COPY_BYTES = 100 * 1024 * 1024


def _tool_command() -> list[str]:
    executable = shutil.which("codegraph.ps1") or shutil.which("codegraph")
    if executable is None:
        raise RuntimeError("CodeGraph CLI 未安装")
    if executable.lower().endswith(".ps1"):
        return ["pwsh", "-NoProfile", "-File", executable]
    return [executable]


def _invoke(*args: str) -> str:
    result = subprocess.run(
        [*_tool_command(), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode:
        raise RuntimeError(f"CodeGraph 失败 ({result.returncode}): {result.stderr[:500]}")
    return result.stdout


def _relative_file(root: Path, name: str) -> Path:
    path = root / name
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError(f"不允许索引仓库外或符号链接文件：{name}")
    return path


@dataclass(frozen=True)
class CodeIndex:
    index_root: Path
    snapshot_id: str
    effective_tree_hash: str
    index_revision: str
    codegraph_version: str
    indexed_files: tuple[str, ...]
    status: dict[str, Any]

    def query(self, text: str, *, limit: int = 20) -> Any:
        return json.loads(_invoke("query", "-p", str(self.index_root), "-l", str(limit),
                                  "-j", text))

    def callees(self, symbol: str, *, limit: int = 20) -> Any:
        return json.loads(_invoke("callees", "-p", str(self.index_root), "-l", str(limit),
                                  "-j", symbol))

    def call_path(
        self, symbols: list[str], *, snapshot_id: str, effective_tree_hash: str,
    ) -> dict[str, Any]:
        """Keep ordered path edges and any additional indexed calls between its nodes."""
        if snapshot_id != self.snapshot_id or effective_tree_hash != self.effective_tree_hash:
            raise ValueError("索引源码指纹与学习快照不一致，必须重建索引")
        nodes: list[dict[str, Any]] = []
        for symbol in symbols:
            if "." not in symbol:
                raise ValueError(f"需要 Class.method 格式：{symbol}")
            klass, method = symbol.rsplit(".", 1)
            matches = [
                item["node"] for item in self.query(symbol)
                if item["node"]["qualifiedName"].endswith(f"::{klass}::{method}")
            ]
            if len(matches) != 1:
                raise ValueError(f"索引中找不到唯一符号：{symbol}")
            node = matches[0]
            path = self.index_root / node["filePath"]
            if not path.resolve().is_relative_to(self.index_root):
                raise ValueError("索引返回仓库外源码路径")
            lines = path.read_text(encoding="utf-8").splitlines()
            if not 1 <= node["startLine"] <= node["endLine"] <= len(lines):
                raise ValueError("索引行号与复制源码不一致")
            nodes.append({"symbol": symbol, "file_path": node["filePath"],
                          "start_line": node["startLine"],
                          "end_line": node["endLine"],
                          "source_hash": hashlib.sha256(path.read_bytes()).hexdigest()})
        edges: list[dict[str, Any]] = []
        for index, left in enumerate(nodes[:-1]):
            callees = self.callees(left["symbol"])["callees"]
            for next_index in range(index + 1, len(nodes)):
                right = nodes[next_index]
                matches = [
                    edge for edge in callees
                    if edge["name"] == right["symbol"].rsplit(".", 1)[-1]
                    and edge["filePath"] == right["file_path"]
                    and edge["startLine"] == right["start_line"]
                ]
                if next_index != index + 1 and not matches:
                    continue
                edges.append({"from": left["symbol"], "to": right["symbol"],
                              "status": "indexed" if matches else "unknown",
                              "evidence": {"file_path": right["file_path"],
                                           "line": right["start_line"]} if matches else None})
        return {"snapshot_id": snapshot_id, "index_revision": self.index_revision,
                "effective_tree_hash": effective_tree_hash,
                "nodes": nodes, "edges": edges}


def build_index(
    repo: Path, *, base_ref: str, selected_untracked: set[str],
    snapshot_id: str, expected: CapturedSnapshot, index_root: Path,
) -> CodeIndex:
    """Copy verified source bytes, index the copy, then recheck the live worktree."""
    root = repository_root(repo)
    index_root = index_root.resolve()
    if index_root == root or index_root.is_relative_to(root):
        raise ValueError("索引目录必须位于用户仓库之外")
    if index_root.exists() and any(index_root.iterdir()):
        raise ValueError("索引目录必须为空，避免复用错误的源码版本")
    before = capture_worktree(root, base_ref, selected_untracked)
    if before.effective_tree_hash != expected.effective_tree_hash:
        raise RuntimeError("工作树与选定 SourceSnapshot 不一致，需要重新捕获")
    tracked = {
        os.fsdecode(item) for item in git(root, "ls-files", "-z", "--cached").split(b"\0")
        if item
    }
    names = sorted(tracked | selected_untracked)
    copied: list[str] = []
    total_size = 0
    index_root.mkdir(parents=True, exist_ok=True)
    for name in names:
        source = _relative_file(root, name)
        if not source.exists():
            continue
        if not source.is_file():
            raise ValueError(f"无法索引非普通文件：{name}")
        size = source.stat().st_size
        total_size += size
        if total_size > MAX_INDEX_COPY_BYTES:
            raise ValueError("索引源码超过本机复制上限")
        destination = index_root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        copied.append(name)
    after = capture_worktree(root, base_ref, selected_untracked)
    if after.effective_tree_hash != before.effective_tree_hash:
        raise RuntimeError("索引复制期间源码已改变，丢弃此次索引")
    version = _invoke("version").strip()
    _invoke("init", "-y", str(index_root))
    status = json.loads(_invoke("status", "-j", str(index_root)))
    revision = hashlib.sha256(
        f"{version}:{before.effective_tree_hash}".encode()
    ).hexdigest()
    return CodeIndex(
        index_root=index_root, snapshot_id=snapshot_id,
        effective_tree_hash=before.effective_tree_hash,
        index_revision=revision, codegraph_version=version,
        indexed_files=tuple(copied), status=status,
    )


def snapshot_source_bytes(ledger: Any, source_ref_id: str) -> bytes:
    row = ledger.connection.execute(
        "SELECT blob_hash, content, deleted FROM source_refs WHERE source_ref_id=?",
        (source_ref_id,),
    ).fetchone()
    if row is None:
        raise ValueError("来源引用不存在")
    content = bytes(row["content"])
    if hashlib.sha256(content).hexdigest() != row["blob_hash"]:
        raise RuntimeError("本机旧快照正文哈希不匹配")
    return content
