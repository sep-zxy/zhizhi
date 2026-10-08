"""Read-only, consistent Git worktree capture for one feature."""

from __future__ import annotations

import difflib
import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

MAX_SOURCE_BYTES = 512 * 1024
MAX_CHANGED_FILES = 80


def git(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )
    return completed.stdout


def git_text(repo: Path, *args: str) -> str:
    return git(repo, *args).decode("utf-8", errors="replace").strip()


def repository_root(path: Path) -> Path:
    root = Path(git_text(path, "rev-parse", "--show-toplevel")).resolve()
    if not root.is_dir():
        raise ValueError("Git 根目录不存在")
    return root


def worktree_id(root: Path) -> str:
    # Linked worktrees share --git-common-dir but have distinct --absolute-git-dir.
    git_dir = Path(git_text(root, "rev-parse", "--absolute-git-dir"))
    return hashlib.sha256(os.path.normcase(str(git_dir.resolve())).encode()).hexdigest()


def _fingerprint(root: Path) -> tuple[str, str, bytes]:
    head = git_text(root, "rev-parse", "HEAD")
    index = Path(git_text(root, "rev-parse", "--git-path", "index"))
    if not index.is_absolute():
        index = root / index
    index_hash = hashlib.sha256(index.read_bytes()).hexdigest() if index.exists() else ""
    status = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    return head, index_hash, status


def _file_names(raw: bytes) -> set[str]:
    return {os.fsdecode(item) for item in raw.split(b"\x00") if item}


def is_internal_state_path(name: str) -> bool:
    return any(part.rstrip(" .").casefold() in {".ahadiff", ".git"} for part in Path(name).parts)


def _safe_file(root: Path, name: str) -> Path:
    if is_internal_state_path(name):
        raise ValueError(f"不允许读取 Git 或 AhaDiff 内部状态：{name}")
    path = root / name
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError(f"不允许读取仓库外或符号链接文件：{name}")
    return path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ChangedSource:
    relative_path: str
    blob_hash: str
    content: bytes
    deleted: bool


@dataclass(frozen=True)
class CapturedSnapshot:
    resolved_base_sha: str
    head_sha: str
    effective_tree_hash: str
    diff_hash: str
    patch_text: str
    changed_sources: tuple[ChangedSource, ...]
    included_untracked: tuple[str, ...]
    filtered_paths: tuple[str, ...]


def _capture_once(root: Path, base_ref: str, selected_untracked: set[str]) -> CapturedSnapshot:
    start_head, start_index, start_status = _fingerprint(root)
    base_commit = git_text(
        root, "rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}"
    )
    base_sha = git_text(root, "merge-base", base_commit, start_head)
    tracked = _file_names(git(root, "ls-files", "-z", "--cached"))
    available_untracked = _file_names(git(root, "ls-files", "-z", "--others", "--exclude-standard"))
    if any(is_internal_state_path(name) for name in selected_untracked):
        raise ValueError("不允许纳入 Git 或 AhaDiff 内部状态文件")
    if not selected_untracked <= available_untracked:
        raise ValueError("所选未跟踪文件不在当前 Git 工作区未跟踪清单中")
    # Git applies attributes and clean filters here, avoiding false changes from
    # checkout-only CRLF conversion. --no-renames exposes both ends of a rename.
    changed_names = (
        _file_names(git(root, "diff", "--no-renames", "--name-only", "-z", base_sha, "--"))
        | selected_untracked
    )
    file_names = sorted(tracked | selected_untracked)
    tree = hashlib.sha256()
    current: dict[str, bytes | None] = {}
    large_hashes: dict[str, str] = {}
    filtered: list[str] = []
    for name in file_names:
        path = _safe_file(root, name)
        if not path.exists():
            current[name] = None
            tree.update(f"{name}\0DELETED\n".encode())
            continue
        if not path.is_file():
            filtered.append(name)
            continue
        size = path.stat().st_size
        if size > MAX_SOURCE_BYTES:
            digest = _file_hash(path)
            large_hashes[name] = digest
            tree.update(name.encode("utf-8") + b"\x00" + digest.encode("ascii") + b"\n")
            filtered.append(name)
            continue
        data = path.read_bytes()
        current[name] = data
        tree.update(name.encode("utf-8") + b"\x00" + _sha256(data).encode("ascii") + b"\n")

    base_names = _file_names(git(root, "ls-tree", "-r", "--name-only", "-z", base_sha))
    changed: list[ChangedSource] = []
    patch_parts: list[str] = []
    for name in sorted(changed_names - set(filtered)):
        before = git(root, "show", f"{base_sha}:{name}") if name in base_names else b""
        after = current.get(name)
        after_bytes = after if after is not None else b""
        if before == after_bytes:
            continue
        if len(before) > MAX_SOURCE_BYTES or b"\x00" in before or b"\x00" in after_bytes:
            filtered.append(name)
            continue
        if len(changed) >= MAX_CHANGED_FILES:
            raise ValueError("变化文件超过上限")
        before_text = before.decode("utf-8", errors="replace").splitlines(keepends=True)
        after_text = after_bytes.decode("utf-8", errors="replace").splitlines(keepends=True)
        old_label = f"a/{name}" if name in base_names else "/dev/null"
        new_label = f"b/{name}" if after is not None else "/dev/null"
        body = "".join(
            difflib.unified_diff(before_text, after_text, fromfile=old_label, tofile=new_label)
        )
        if body:
            patch_parts.append(f"diff --git a/{name} b/{name}\n{body}")
            changed.append(ChangedSource(name, _sha256(after_bytes), after_bytes, after is None))

    end_head, end_index, end_status = _fingerprint(root)
    if (start_head, start_index, start_status) != (end_head, end_index, end_status):
        raise RuntimeError("Git 工作区在捕获期间变化")
    for name, data in current.items():
        path = _safe_file(root, name)
        if (path.read_bytes() if path.exists() else None) != data:
            raise RuntimeError("源码在捕获期间变化")
    for name, digest in large_hashes.items():
        path = _safe_file(root, name)
        if not path.exists() or _file_hash(path) != digest:
            raise RuntimeError("大文件在捕获期间变化")
    patch_text = "".join(patch_parts)
    return CapturedSnapshot(
        resolved_base_sha=base_sha,
        head_sha=start_head,
        effective_tree_hash=tree.hexdigest(),
        diff_hash=_sha256(patch_text.encode("utf-8")),
        patch_text=patch_text,
        changed_sources=tuple(changed),
        included_untracked=tuple(sorted(selected_untracked)),
        filtered_paths=tuple(sorted(set(filtered))),
    )


def capture_worktree(
    root: Path, base_ref: str, selected_untracked: set[str] | None = None
) -> CapturedSnapshot:
    """Capture the effective worktree without changing HEAD, index or files."""
    root = repository_root(root)
    selected = selected_untracked or set()
    for attempt in range(2):
        try:
            return _capture_once(root, base_ref, selected)
        except RuntimeError:
            if attempt:
                raise
    raise AssertionError("unreachable")


def capture_commit(root: Path, revision: str) -> CapturedSnapshot:
    """Capture one immutable commit, without reading later worktree changes."""
    root = repository_root(root)
    sha = git_text(root, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}")
    parents = git_text(root, "rev-list", "--parents", "-n", "1", sha).split()
    base = parents[1] if len(parents) > 1 else git_text(root, "hash-object", "-t", "tree", "--stdin")
    names = _file_names(git(root, "diff", "--no-renames", "--name-only", "-z", base, sha, "--"))
    if len(names) > MAX_CHANGED_FILES:
        raise ValueError("提交变化文件超过上限")
    base_names = _file_names(git(root, "ls-tree", "-r", "--name-only", "-z", base))
    head_names = _file_names(git(root, "ls-tree", "-r", "--name-only", "-z", sha))
    changed: list[ChangedSource] = []
    patches: list[str] = []
    filtered: list[str] = []
    for name in sorted(names):
        if is_internal_state_path(name):
            filtered.append(name)
            continue
        before = git(root, "show", f"{base}:{name}") if name in base_names else b""
        after = git(root, "show", f"{sha}:{name}") if name in head_names else b""
        if (len(before) > MAX_SOURCE_BYTES or len(after) > MAX_SOURCE_BYTES
                or b"\x00" in before or b"\x00" in after):
            filtered.append(name)
            continue
        body = "".join(difflib.unified_diff(
            before.decode("utf-8", errors="replace").splitlines(keepends=True),
            after.decode("utf-8", errors="replace").splitlines(keepends=True),
            fromfile=f"a/{name}" if name in base_names else "/dev/null",
            tofile=f"b/{name}" if name in head_names else "/dev/null",
        ))
        if body:
            patches.append(f"diff --git a/{name} b/{name}\n{body}")
            changed.append(ChangedSource(name, _sha256(after), after, name not in head_names))
    patch_text = "".join(patches)
    return CapturedSnapshot(
        resolved_base_sha=base,
        head_sha=sha,
        effective_tree_hash=git_text(root, "rev-parse", f"{sha}^{{tree}}"),
        diff_hash=_sha256(patch_text.encode("utf-8")),
        patch_text=patch_text,
        changed_sources=tuple(changed),
        included_untracked=(),
        filtered_paths=tuple(filtered),
    )
