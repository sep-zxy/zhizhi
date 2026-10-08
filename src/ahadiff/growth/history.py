"""Local, read-only Git history index for a bound project."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .git_snapshot import git

if TYPE_CHECKING:
    from .local import GrowthLocalRepository


def _category(title: str, paths: list[str]) -> str:
    lower = title.casefold()
    if any(word in lower for word in ("fix", "bug", "修复", "错误")):
        return "修复问题"
    if any(word in lower for word in ("test", "测试")) or paths and all(
        "test" in path.casefold() for path in paths
    ):
        return "测试验证"
    if paths and all(path.endswith((".md", ".rst", ".txt")) for path in paths):
        return "文档说明"
    if any(word in lower for word in ("refactor", "重构", "整理", "优化")):
        return "结构调整"
    if paths and all(path.startswith((".github/", "scripts/", "acceptance/"))
                     for path in paths):
        return "工程配置"
    return "功能变化"


def _read_commits(root: Path) -> list[dict[str, Any]]:
    raw = git(
        root, "-c", "core.quotePath=false", "log", "--all", "--diff-merges=first-parent",
        "--format=%x1e%H%x1f%s%x1f%an%x1f%ae%x1f%aI", "--name-only",
    ).decode("utf-8", errors="replace")
    result: list[dict[str, Any]] = []
    for block in raw.split("\x1e"):
        if not block.strip():
            continue
        lines = block.strip("\r\n").splitlines()
        fields = lines[0].split("\x1f")
        if len(fields) != 5 or len(fields[0]) != 40:
            raise ValueError("Git 历史记录格式无效")
        sha, title, author_name, author_email, authored_at = fields
        paths = [path for path in lines[1:] if path]
        result.append({
            "sha": sha, "title": title, "author_name": author_name,
            "author_email": author_email, "authored_at": authored_at,
            "paths": paths, "category": _category(title, paths),
        })
    return result


def scan_history(ledger: GrowthLocalRepository, binding_id: str) -> dict[str, int]:
    binding = ledger.connection.execute(
        "SELECT canonical_local_path FROM bindings WHERE binding_id=?", (binding_id,),
    ).fetchone()
    if binding is None:
        raise ValueError("仓库绑定不存在")
    commits = _read_commits(Path(binding["canonical_local_path"]))
    previous = ledger.connection.execute(
        "SELECT scan_sequence FROM git_history_scans WHERE binding_id=?", (binding_id,),
    ).fetchone()
    sequence = int(previous["scan_sequence"]) + 1 if previous else 1
    now = datetime.now(UTC).isoformat()
    discovered = 0
    with ledger.connection:
        ledger.connection.execute(
            "UPDATE git_history_commits SET reachable=0 WHERE binding_id=?", (binding_id,),
        )
        for commit in commits:
            exists = ledger.connection.execute(
                "SELECT 1 FROM git_history_commits WHERE binding_id=? AND commit_sha=?",
                (binding_id, commit["sha"]),
            ).fetchone()
            discovered += int(exists is None)
            ledger.connection.execute(
                "INSERT INTO git_history_commits "
                "(binding_id,commit_sha,title,author_name,author_email,authored_at,"
                "paths_json,category,first_seen_scan,last_seen_scan,reachable) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,1) "
                "ON CONFLICT(binding_id,commit_sha) DO UPDATE SET "
                "title=excluded.title,author_name=excluded.author_name,"
                "author_email=excluded.author_email,authored_at=excluded.authored_at,"
                "paths_json=excluded.paths_json,category=excluded.category,"
                "last_seen_scan=excluded.last_seen_scan,reachable=1",
                (binding_id, commit["sha"], commit["title"], commit["author_name"],
                 commit["author_email"], commit["authored_at"],
                 json.dumps(commit["paths"], ensure_ascii=False), commit["category"],
                 sequence, sequence),
            )
        ledger.connection.execute(
            "INSERT INTO git_history_scans VALUES (?,?,?) "
            "ON CONFLICT(binding_id) DO UPDATE SET "
            "scan_sequence=excluded.scan_sequence,scanned_at=excluded.scanned_at",
            (binding_id, sequence, now),
        )
    return {"scan_sequence": sequence, "reachable_count": len(commits),
            "discovered_count": discovered if sequence > 1 else 0}


def list_history(
    ledger: GrowthLocalRepository, binding_id: str, *, cursor: int = 0,
    limit: int = 50, author_emails: list[str] | None = None,
    author_groups: list[str] | None = None,
) -> dict[str, Any]:
    if cursor < 0 or limit < 1 or limit > 200:
        raise ValueError("提交分页参数无效")
    scan = ledger.connection.execute(
        "SELECT scan_sequence,scanned_at FROM git_history_scans WHERE binding_id=?",
        (binding_id,),
    ).fetchone()
    if scan is None:
        scan_result = scan_history(ledger, binding_id)
        scan = ledger.connection.execute(
            "SELECT scan_sequence,scanned_at FROM git_history_scans WHERE binding_id=?",
            (binding_id,),
        ).fetchone()
        assert scan is not None
    else:
        scan_result = {"scan_sequence": scan["scan_sequence"],
                       "reachable_count": 0, "discovered_count": 0}
    clauses = ["commits.binding_id=?", "commits.reachable=1"]
    params: list[Any] = [binding_id]
    if author_emails:
        clauses.append("commits.author_email IN (" + ",".join("?" for _ in author_emails) + ")")
        params.extend(author_emails)
    if author_groups:
        clauses.append("COALESCE(ids.group_name,commits.author_email) IN ("
                       + ",".join("?" for _ in author_groups) + ")")
        params.extend(author_groups)
    where = " AND ".join(clauses)
    rows = ledger.connection.execute(
        "SELECT commits.*, COALESCE(ids.group_name,commits.author_email) AS author_group, "
        "(SELECT COUNT(*) FROM card_source_commits AS links "
        "WHERE links.binding_id=commits.binding_id AND links.commit_sha=commits.commit_sha) "
        "AS linked_card_count FROM git_history_commits AS commits "
        "LEFT JOIN git_author_identities AS ids ON ids.binding_id=commits.binding_id "
        "AND ids.author_email=commits.author_email WHERE " + where +
        " ORDER BY commits.authored_at DESC,commits.commit_sha DESC LIMIT ? OFFSET ?",
        (*params, limit + 1, cursor),
    ).fetchall()
    commits = [{
        "sha": row["commit_sha"], "title": row["title"],
        "author_name": row["author_name"], "author_email": row["author_email"],
        "author_group": row["author_group"], "authored_at": row["authored_at"],
        "paths": json.loads(row["paths_json"]), "category": row["category"],
        "is_new": scan["scan_sequence"] > 1
        and row["first_seen_scan"] == scan["scan_sequence"],
        "learning_status": "learned" if row["linked_card_count"] else "unlearned",
        "linked_card_count": row["linked_card_count"],
    } for row in rows[:limit]]
    identities = [dict(row) for row in ledger.connection.execute(
        "SELECT commits.author_name,commits.author_email, "
        "COALESCE(ids.group_name,commits.author_email) AS group_name, "
        "COUNT(*) AS commit_count FROM git_history_commits AS commits "
        "LEFT JOIN git_author_identities AS ids ON ids.binding_id=commits.binding_id "
        "AND ids.author_email=commits.author_email "
        "WHERE commits.binding_id=? AND commits.reachable=1 "
        "GROUP BY commits.author_name,commits.author_email,group_name "
        "ORDER BY commit_count DESC,commits.author_email",
        (binding_id,),
    )]
    return {"commits": commits, "next_cursor": str(cursor + limit)
            if len(rows) > limit else None, "authors": identities,
            "scan": dict(scan) | {"discovered_count": scan_result["discovered_count"]}}


def group_authors(
    ledger: GrowthLocalRepository, binding_id: str, *,
    author_emails: list[str], group_name: str,
) -> dict[str, Any]:
    if not group_name.strip() or not author_emails or len(author_emails) > 20:
        raise ValueError("作者身份分组不能为空")
    known = {row[0] for row in ledger.connection.execute(
        "SELECT DISTINCT author_email FROM git_history_commits WHERE binding_id=?",
        (binding_id,),
    )}
    if not set(author_emails) <= known:
        raise ValueError("作者身份不属于当前仓库")
    with ledger.connection:
        for email in set(author_emails):
            ledger.connection.execute(
                "INSERT INTO git_author_identities VALUES (?,?,?) "
                "ON CONFLICT(binding_id,author_email) DO UPDATE SET "
                "group_name=excluded.group_name",
                (binding_id, email, group_name.strip()),
            )
    return {"group_name": group_name.strip(), "author_emails": sorted(set(author_emails))}
