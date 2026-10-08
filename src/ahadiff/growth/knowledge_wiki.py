"""Reviewed Obsidian Wiki drafts with stable concept identity and safe file updates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .knowledge_learning import card_detail
from .local import GrowthLocalRepository


_START = "<!-- growth:managed:start -->"
_END = "<!-- growth:managed:end -->"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _root(ledger: GrowthLocalRepository) -> Path | None:
    row = ledger.connection.execute(
        "SELECT vault_path FROM knowledge_vault_settings WHERE setting_id=1"
    ).fetchone()
    return Path(row[0]).resolve() if row else None


def _folder(ledger: GrowthLocalRepository) -> str:
    row = ledger.connection.execute(
        "SELECT target_folder FROM knowledge_vault_settings WHERE setting_id=1"
    ).fetchone()
    return str(row[0]) if row else "工程成长伴侣"


def _validated_folder(value: str) -> str:
    raw = value.strip()
    if not raw or len(raw) > 256 or raw.startswith(("/", "\\")) or ":" in raw:
        raise ValueError("Wiki 目标文件夹必须是 vault 内的相对路径")
    if raw == ".":
        return raw
    parts = raw.replace("\\", "/").split("/")
    if any(
        not part or part in {".", ".."} or part.endswith((" ", "."))
        or re.search(r'[<>:"|?*\x00-\x1f]', part)
        for part in parts
    ):
        raise ValueError("Wiki 目标文件夹包含无效路径片段")
    return "/".join(parts)


def vault(ledger: GrowthLocalRepository) -> dict[str, Any]:
    root = _root(ledger)
    return {"path": str(root) if root else None,
            "target_folder": _folder(ledger),
            "configured": bool(root and root.is_dir()),
            "obsidian_detected": bool(root and (root / ".obsidian").is_dir())}


def set_vault(
    ledger: GrowthLocalRepository, path: str, target_folder: str | None = None,
) -> dict[str, Any]:
    root = Path(path).expanduser().resolve()
    if not root.is_dir() or not (root / ".obsidian").is_dir():
        raise ValueError("请选择已有的 Obsidian vault 目录")
    folder = _validated_folder(target_folder if target_folder is not None else _folder(ledger))
    if folder != ".":
        _target(root, folder)
    with ledger.connection:
        ledger.connection.execute(
            "INSERT INTO knowledge_vault_settings "
            "(setting_id,vault_path,target_folder,updated_at) VALUES (1,?,?,?) "
            "ON CONFLICT(setting_id) DO UPDATE SET vault_path=excluded.vault_path,"
            "target_folder=excluded.target_folder,updated_at=excluded.updated_at",
            (str(root), folder, _now()),
        )
    return vault(ledger)


def _target(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("Wiki 目标路径无效")
    root = root.resolve(strict=True)
    target = (root / relative).resolve(strict=False)
    if not target.is_relative_to(root):
        raise ValueError("Wiki 目标路径越过 vault 边界")
    return target


def _relative_name(title: str, concept_id: str, folder: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", title).strip(" .-")[:70]
    safe = safe or "知识点"
    name = f"{safe}-{concept_id[:8]}.md"
    return f"{folder}/{name}" if folder != "." else name


def _managed_markdown(detail: dict[str, Any]) -> str:
    card = detail["card"]
    material = card["material"]
    if material is None:
        raise ValueError("知识讲解尚未生成")
    answers = detail["learning"]["answers"]
    parts = [
        _START,
        "## 一句话定义",
        material["back_summary"].strip(),
        "## 解决的问题",
        str(card["learning_goal"]).strip(),
        "## 何时使用与适用边界",
        material["back_mechanism"].strip(),
        "**边界：**" + material["back_boundary"].strip(),
        "## 这次代码中的实现",
    ]
    paths = list(dict.fromkeys(
        str(source["relative_path"]) for source in detail["sources"]
        if source.get("relative_path")
    ))
    parts.extend(f"- `{path}`" for path in paths)
    parts.extend(["## 错误理解与修正"])
    for question in detail["questions"]:
        kind = question["kind"]
        answer = answers.get(kind)
        if not answer or not answer.get("explanation_confirmed"):
            raise ValueError("两题解析尚未完整确认")
        if not answer["correct"]:
            parts.append(
                f"- {('理解题' if kind == 'understanding' else '预测题')}首次选了 "
                f"{answer['option_id']}，这次选择有误。该选项："
                f"{question['explanations'][answer['option_id']]} "
                f"正确项 {question['correct_option_id']}："
                f"{question['explanations'][question['correct_option_id']]}"
            )
        else:
            parts.append(
                f"- {('理解题' if kind == 'understanding' else '预测题')}首次选择 "
                f"{answer['option_id']}，判定正确。依据：{question['reasoning']}"
            )
    parts.extend(["## 常见误区"])
    parts.extend("- " + value for value in material["misconceptions"])
    prediction = next(item for item in detail["questions"]
                      if item["kind"] == "prediction")
    parts.extend([
        "## 最小可迁移实例",
        prediction["scenario"],
        prediction["reasoning"],
        "**条件边界：**" + prediction["boundary"],
        "## 工程证据时间线",
    ])
    for source in detail["sources"]:
        if not source.get("commit_sha"):
            continue
        date = str(source.get("authored_at") or "时间未记录")[:10]
        label = str(source.get("commit_title") or "提交标题未记录")
        parts.append(
            f"- {date} · 项目 `{source.get('project_id') or '未知'}` · "
            f"提交 `{source['commit_sha']}` · {label}。"
        )
    parts.extend([
        "源文件：" + "、".join(f"`{path}`" for path in paths) + "。",
        "## 回到学习卡",
        f"- 卡片 ID：`{card['card_id']}`",
        f"- 成长伴侣：`growth://cards/{card['card_id']}`",
        _END,
    ])
    return "\n\n".join(parts) + "\n"


def _draft_markdown(detail: dict[str, Any], original: str | None) -> str:
    managed = _managed_markdown(detail)
    if original is not None:
        if original.count(_START) != 1 or original.count(_END) != 1:
            raise ValueError("既有 Wiki 缺少应用管理段落，请先人工核对")
        start = original.index(_START)
        end = original.index(_END) + len(_END)
        return original[:start] + managed.rstrip() + original[end:]
    card = detail["card"]
    title = str(card["title"] or card["learning_goal"])
    frontmatter = (
        "---\n" +
        f"growth_concept_id: {json.dumps(card['concept_id'], ensure_ascii=False)}\n" +
        f"title: {json.dumps(title, ensure_ascii=False)}\n" +
        f"growth_card_id: {json.dumps(card['card_id'], ensure_ascii=False)}\n" +
        f"updated: {datetime.now().astimezone().date().isoformat()}\n" +
        "---\n\n"
    )
    note = detail["learning"]["note_text"]
    return (frontmatter + f"# {title}\n\n" + managed +
            "\n## 我的补充与待验证问题\n\n" + (note.strip() or "（由我自己补充）") + "\n")


def articles(ledger: GrowthLocalRepository) -> list[dict[str, Any]]:
    root = _root(ledger)
    result = []
    for row in ledger.connection.execute(
        "SELECT articles.*,concepts.title FROM knowledge_wiki_articles AS articles "
        "JOIN knowledge_concepts AS concepts USING(concept_id) "
        "ORDER BY articles.published_at DESC"
    ):
        item = dict(row)
        item["markdown"] = None
        item["external_modified"] = False
        if root and root.is_dir():
            try:
                target = _target(root, item["relative_path"])
                if target.is_file():
                    item["markdown"] = target.read_text(encoding="utf-8")
                    item["external_modified"] = _digest(item["markdown"]) != item["file_hash"]
            except (OSError, ValueError):
                pass
        result.append(item)
    return result


def latest_draft(ledger: GrowthLocalRepository, card_id: str) -> dict[str, Any] | None:
    row = ledger.connection.execute(
        "SELECT * FROM knowledge_wiki_drafts WHERE card_id=? "
        "ORDER BY created_at DESC LIMIT 1", (card_id,),
    ).fetchone()
    return dict(row) if row else None


def create_draft(
    ledger: GrowthLocalRepository, card_id: str, *, request_id: str,
) -> dict[str, Any]:
    existing = ledger.connection.execute(
        "SELECT * FROM knowledge_wiki_drafts WHERE request_id=?", (request_id,),
    ).fetchone()
    if existing:
        if existing["card_id"] != card_id:
            raise ValueError("同一草稿请求不能更换卡片")
        return dict(existing)
    detail = card_detail(ledger, card_id)
    if not detail["learning"]["completed_at"]:
        raise ValueError("只有完成讲解、两题与解析后才能提炼 Wiki")
    concept_id = detail["card"]["concept_id"]
    if not concept_id:
        raise ValueError("卡片缺少稳定概念身份")
    article = ledger.connection.execute(
        "SELECT * FROM knowledge_wiki_articles WHERE concept_id=?", (concept_id,),
    ).fetchone()
    relative = (str(article["relative_path"]) if article else
                _relative_name(str(detail["card"]["title"] or detail["card"]["learning_goal"]),
                               concept_id, _folder(ledger)))
    root = _root(ledger)
    original = None
    if root and root.is_dir():
        target = _target(root, relative)
        if article and not target.is_file():
            raise ValueError("原 Wiki 文件已移动或丢失，请先重新定位")
        if target.exists():
            if not article:
                raise ValueError("Wiki 目标已有无归属文件，不能覆盖")
            original = target.read_text(encoding="utf-8")
    elif article:
        raise ValueError("此设备未配置原 Wiki 所在 vault，请先配置")
    markdown = _draft_markdown(detail, original)
    draft_id = str(uuid.uuid4())
    with ledger.connection:
        ledger.connection.execute(
            "INSERT INTO knowledge_wiki_drafts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (draft_id, concept_id, card_id, request_id, relative,
             _digest(original) if original is not None else None,
             original, markdown, "draft", None, _now(), _now()),
        )
    return latest_draft(ledger, card_id) or {}


def update_draft(
    ledger: GrowthLocalRepository, draft_id: str, markdown: str,
) -> dict[str, Any]:
    if not markdown.strip() or len(markdown) > 200000:
        raise ValueError("Wiki 草稿为空或过长")
    row = ledger.connection.execute(
        "SELECT * FROM knowledge_wiki_drafts WHERE draft_id=?", (draft_id,),
    ).fetchone()
    if row is None or row["status"] != "draft":
        raise ValueError("草稿不存在或已经发布")
    if markdown.count(_START) != 1 or markdown.count(_END) != 1:
        raise ValueError("草稿必须保留应用管理段落边界")
    with ledger.connection:
        ledger.connection.execute(
            "UPDATE knowledge_wiki_drafts SET markdown=?,updated_at=? WHERE draft_id=?",
            (markdown, _now(), draft_id),
        )
    return dict(ledger.connection.execute(
        "SELECT * FROM knowledge_wiki_drafts WHERE draft_id=?", (draft_id,),
    ).fetchone())


def publish_draft(
    ledger: GrowthLocalRepository, draft_id: str, *, request_id: str,
) -> dict[str, Any]:
    draft = ledger.connection.execute(
        "SELECT * FROM knowledge_wiki_drafts WHERE draft_id=?", (draft_id,),
    ).fetchone()
    if draft is None:
        raise ValueError("Wiki 草稿不存在")
    article = ledger.connection.execute(
        "SELECT * FROM knowledge_wiki_articles WHERE concept_id=?",
        (draft["concept_id"],),
    ).fetchone()
    if draft["status"] == "published":
        if draft["publish_request_id"] != request_id:
            raise ValueError("草稿已由另一请求发布")
        return dict(article) if article else {}
    root = _root(ledger)
    if root is None or not root.is_dir():
        raise ValueError("请先配置此设备上的 Obsidian vault")
    target = _target(root, draft["target_relative_path"])
    current = target.read_text(encoding="utf-8") if target.is_file() else None
    current_hash = _digest(current) if current is not None else None
    desired_hash = _digest(draft["markdown"])
    if current_hash != draft["original_hash"] and current_hash != desired_hash:
        raise ValueError("Wiki 在草稿审阅期间已被外部修改，请重新生成并核对差异")
    if article and article["relative_path"] != draft["target_relative_path"]:
        raise ValueError("概念的 Wiki 路径已改变，请重新定位")
    if not article and current is not None and current_hash != desired_hash:
        raise ValueError("目标已有其他文件，不能覆盖")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    if not parent.resolve().is_relative_to(root):
        raise ValueError("Wiki 目标父目录越过 vault 边界")
    if current_hash != desired_hash:
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", delete=False,
                dir=parent, prefix=".growth-", suffix=".tmp",
            ) as handle:
                handle.write(draft["markdown"])
                handle.flush()
                os.fsync(handle.fileno())
                temporary = handle.name
            os.replace(temporary, target)
        finally:
            if temporary and Path(temporary).exists():
                Path(temporary).unlink()
    with ledger.connection:
        if article:
            ledger.connection.execute(
                "UPDATE knowledge_wiki_articles SET file_hash=?,version=version+1,"
                "published_at=? WHERE concept_id=?",
                (desired_hash, _now(), draft["concept_id"]),
            )
        else:
            ledger.connection.execute(
                "INSERT INTO knowledge_wiki_articles VALUES (?,?,?,?,?,?)",
                (draft["concept_id"], str(uuid.uuid4()),
                 draft["target_relative_path"], desired_hash, 1, _now()),
            )
        ledger.connection.execute(
            "UPDATE knowledge_wiki_drafts SET status='published',"
            "publish_request_id=?,updated_at=? WHERE draft_id=?",
            (request_id, _now(), draft_id),
        )
    return dict(ledger.connection.execute(
        "SELECT * FROM knowledge_wiki_articles WHERE concept_id=?",
        (draft["concept_id"],),
    ).fetchone())
