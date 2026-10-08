"""Export formal growth knowledge cards as a one-way Anki package."""

from __future__ import annotations

import hashlib
import html
import json
import tempfile
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .local import GrowthLocalRepository


def _stable_number(value: str) -> int:
    return int(hashlib.sha256(value.encode()).hexdigest()[:8], 16)


def _html(value: str) -> str:
    return html.escape(value).replace("\n", "<br>")


def export_knowledge_cards_apkg(
    ledger: GrowthLocalRepository, project_id: str | None = None,
) -> bytes:
    try:
        genanki: Any = import_module("genanki")
    except ImportError as exc:
        raise ValueError("genanki 未安装，无法导出知识卡") from exc
    params: tuple[str, ...] = (project_id, project_id) if project_id else ()
    where = (
        "AND (features.project_id=? OR EXISTS ("
        "SELECT 1 FROM card_source_commits AS links "
        "JOIN bindings ON bindings.binding_id=links.binding_id "
        "WHERE links.card_id=cards.card_id AND bindings.project_id=?))"
    ) if project_id else ""
    rows = ledger.connection.execute(
        "SELECT cards.card_id,cards.learning_goal,cards.back_answer,"
        "cards.back_explanation,tasks.question,tasks.topic_id,features.project_id,"
        "projects.name AS project_name,tasks.source_refs_json "
        "FROM knowledge_cards AS cards JOIN growth_tasks AS tasks "
        "ON tasks.task_id=cards.card_id JOIN opportunities USING(opportunity_id) "
        "JOIN analysis_runs USING(analysis_id) JOIN snapshots USING(snapshot_id) "
        "JOIN features USING(feature_id) JOIN projects USING(project_id) "
        "WHERE tasks.progress!='dismissed' " + where +
        " ORDER BY features.project_id,cards.created_at,cards.card_id LIMIT 10001",
        params,
    ).fetchall()
    if len(rows) > 10000:
        raise ValueError("单次最多导出 10000 张正式知识卡")
    eligible = [row for row in rows if row["back_answer"].strip()]
    if not eligible:
        raise ValueError("当前项目尚无背面答案完整的正式知识卡")
    project_name = None
    if project_id is not None:
        project = ledger.connection.execute(
            "SELECT name FROM projects WHERE project_id=?", (project_id,),
        ).fetchone()
        if project is None:
            raise ValueError("项目不存在")
        project_name = str(project["name"])
    deck_name = "工程成长伴侣" if project_name is None else f"工程成长伴侣 · {project_name}"
    deck = genanki.Deck(_stable_number("growth-deck:" + (project_id or "all")), deck_name)
    model = genanki.Model(
        _stable_number("growth-knowledge-card-v1"), "工程成长伴侣知识卡",
        fields=[{"name": "Front"}, {"name": "Back"}, {"name": "Sources"}],
        templates=[{
            "name": "知识卡", "qfmt": "{{Front}}",
            "afmt": '{{FrontSide}}<hr id="answer">{{Back}}<div class="sources">{{Sources}}</div>',
        }],
        css=".card{font-family:sans-serif;font-size:18px;line-height:1.6;}"
            ".sources{margin-top:1.5em;font-size:12px;color:#666;}",
    )
    for row in eligible:
        shas = [item[0] for item in ledger.connection.execute(
            "SELECT commit_sha FROM card_source_commits WHERE card_id=? "
            "ORDER BY created_at,commit_sha", (row["card_id"],),
        )]
        refs = json.loads(row["source_refs_json"])
        paths = [item[0] for item in ledger.connection.execute(
            "SELECT relative_path FROM source_refs WHERE source_ref_id IN ("
            + ",".join("?" for _ in refs) + ") ORDER BY relative_path",
            refs,
        )] if refs else []
        answer = _html(str(row["back_answer"]))
        if row["back_explanation"]:
            answer += "<hr>" + _html(str(row["back_explanation"]))
        source_text = " · ".join([*paths, *(sha[:12] for sha in shas)])
        deck.add_note(genanki.Note(
            model=model,
            fields=[_html(str(row["question"])), answer, _html(source_text)],
            guid=genanki.guid_for(str(row["card_id"])),
            tags=["growth-companion"],
        ))
    package = genanki.Package(deck)
    with tempfile.TemporaryDirectory(prefix="growth-cards-apkg-") as temp_dir:
        output = Path(temp_dir) / "growth-cards.apkg"
        package.write_to_file(str(output))
        return output.read_bytes()
