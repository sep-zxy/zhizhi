"""Release story preparation and strict evidence verification.

This module never converts a replay or an incomplete live run into a PASS.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

STEPS = {"S01": 11, "S02": 13, "S03": 12, "S04": 15}
STATUSES = {"NOT_RUN", "RUNNING", "PASS", "FAIL", "BLOCKED"}
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,79}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
PROOF_NAMES = (
    "real_environment",
    "business_assertions",
    "semantic_review",
    "complete_chain",
    "windows_app",
    "macos_app",
    "cloud_postgres",
    "model_provider",
    "reme",
)

TS_CONFIG = {
    "compilerOptions": {
        "target": "ES2022",
        "module": "NodeNext",
        "moduleResolution": "NodeNext",
        "strict": True,
        "rootDir": "src",
        "outDir": "dist",
        "lib": ["ES2022"],
        "types": [],
    },
    "include": ["src/**/*.ts"],
}

FIXTURES: dict[str, dict[str, Any]] = {
    "S01": {
        "project": "route-demo",
        "branch": "feature/parallel-route",
        "commit": "建立路线查询模拟基线",
        "baseline": {
            "src/demo/route/RoutePlanner.java": """package demo.route;
import java.util.concurrent.Executor;
import java.util.function.Function;
public final class RoutePlanner {
    public String plan(Function<String, String> query, Executor executor) {
        String a = query.apply("A");
        String b = query.apply("B");
        return a + "|" + b;
    }
}
""",
        },
        "next": {
            "src/demo/route/RoutePlanner.java": """package demo.route;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executor;
import java.util.function.Function;
public final class RoutePlanner {
    public String plan(Function<String, String> query, Executor executor) {
        CompletableFuture<String> a = CompletableFuture.supplyAsync(
            () -> query.apply("A"), executor);
        CompletableFuture<String> b = CompletableFuture.supplyAsync(
            () -> query.apply("B"), executor);
        return a.thenCombine(b, (left, right) -> left + "|" + right).join();
    }
}
""",
        },
    },
    "S02": {
        "project": "search-demo",
        "branch": "fix/search-response-order",
        "commit": "建立搜索竞态模拟基线",
        "baseline": {
            "src/search.ts": """export type FetchResults = (query: string) => Promise<string[]>;
export function createSearch(
  fetchResults: FetchResults,
  render: (rows: string[]) => void,
) {
  return async (query: string): Promise<void> => {
    const rows = await fetchResults(query);
    render(rows);
  };
}
""",
        },
        "next": {
            "src/search.ts": """export type FetchResults = (query: string) => Promise<string[]>;
export function createSearch(
  fetchResults: FetchResults,
  render: (rows: string[]) => void,
) {
  let latestRequest = 0;
  return async (query: string): Promise<void> => {
    const requestId = ++latestRequest;
    const rows = await fetchResults(query);
    if (requestId === latestRequest) {
      render(rows);
    }
  };
}
""",
        },
    },
    "S03": {
        "project": "order-demo",
        "branch": "feature/price-cache",
        "commit": "建立订单计价模块模拟基线",
        "baseline": {
            "src/demo/orders/PricingGateway.java": """package demo.orders;
public interface PricingGateway {
    int priceCents(String sku);
}
""",
            "src/demo/orders/OrderService.java": """package demo.orders;
import java.util.function.LongSupplier;
public final class OrderService {
    private final PricingGateway gateway;
    public OrderService(PricingGateway gateway, LongSupplier clock) {
        this.gateway = gateway;
    }
    public int quote(String sku, int quantity) {
        return gateway.priceCents(sku) * quantity;
    }
}
""",
            "src/demo/orders/OrderController.java": """package demo.orders;
public final class OrderController {
    private final OrderService service;
    public OrderController(OrderService service) {
        this.service = service;
    }
    public int quote(String sku, int quantity) {
        return service.quote(sku, quantity);
    }
}
""",
        },
        "next": {
            "src/demo/orders/PriceCache.java": """package demo.orders;
import java.util.HashMap;
import java.util.Map;
import java.util.function.IntSupplier;
import java.util.function.LongSupplier;
public final class PriceCache {
    private record Entry(int price, long expiresAt) {}
    private final Map<String, Entry> entries = new HashMap<>();
    private final LongSupplier clock;
    private final long ttlMillis;
    public PriceCache(LongSupplier clock, long ttlMillis) {
        this.clock = clock;
        this.ttlMillis = ttlMillis;
    }
    public int get(String sku, IntSupplier load) {
        long now = clock.getAsLong();
        Entry old = entries.get(sku);
        if (old != null && now < old.expiresAt()) {
            return old.price();
        }
        int price = load.getAsInt();
        entries.put(sku, new Entry(price, now + ttlMillis));
        return price;
    }
}
""",
            "src/demo/orders/OrderService.java": """package demo.orders;
import java.util.function.LongSupplier;
public final class OrderService {
    private final PricingGateway gateway;
    private final PriceCache cache;
    public OrderService(PricingGateway gateway, LongSupplier clock) {
        this.gateway = gateway;
        this.cache = new PriceCache(clock, 1000);
    }
    public int quote(String sku, int quantity) {
        int price = cache.get(sku, () -> gateway.priceCents(sku));
        return price * quantity;
    }
}
""",
        },
    },
    "S04": {
        "project": "document-demo",
        "branch": "fix/document-owner-check",
        "commit": "建立文档权限模拟基线",
        "baseline": {
            "src/models.ts": """export type User = { id: string };
export type Document = { ownerId: string; content: string };
""",
            "src/document.ts": """import type { User, Document } from "./models.js";
export function updateContent(
  user: User | null,
  doc: Document,
  content: string,
): Document {
  if (!user) throw new Error("UNAUTHENTICATED");
  return { ...doc, content };
}
""",
        },
        "next": {
            "src/access.ts": """import type { User, Document } from "./models.js";
export function canEdit(user: User | null, doc: Document): boolean {
  return user !== null && user.id === doc.ownerId;
}
""",
            "src/document.ts": """import type { User, Document } from "./models.js";
import { canEdit } from "./access.js";
export function updateContent(
  user: User | null,
  doc: Document,
  content: string,
): Document {
  if (!user) throw new Error("UNAUTHENTICATED");
  if (!canEdit(user, doc)) throw new Error("FORBIDDEN");
  return { ...doc, content };
}
""",
        },
    },
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点必须是对象：{path}")
    return cast("dict[str, Any]", value)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle_has_base(bundle: Path, base_sha: str) -> bool:
    listed = subprocess.run(
        ["git", "bundle", "list-heads", str(bundle)], cwd=bundle.parent,
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    return listed.returncode == 0 and (
        f"{base_sha} refs/heads/main" in listed.stdout.splitlines()
    )


def _run_dir(root: Path, run_id: str) -> Path:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("run_id 只能由字母、数字、下划线和连字符组成，长度 3–80")
    return root.resolve() / run_id


def prepare(
    root: Path,
    case: str,
    run_id: str,
    release: str,
    supersedes_run_id: str | None = None,
    candidate_sha256: str | None = None,
) -> dict[str, Any]:
    run_dir = _run_dir(root, run_id)
    if run_dir.exists():
        raise ValueError("run_id 已存在；失败重跑必须使用新的 run_id")
    if supersedes_run_id is not None:
        old_dir = _run_dir(root, supersedes_run_id)
        old_manifest = _load(old_dir / "manifest.json")
        if old_manifest.get("case_id") != case or old_manifest.get("release") != release:
            raise ValueError("被替代运行必须属于同一 case 和发布候选")
        if _load(old_dir / "result.json").get("status") == "PASS":
            raise ValueError("不能替代已通过的情境运行")
    fixture = FIXTURES[case]
    repo = run_dir / "fixture" / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "情境验证")
    _git(repo, "config", "user.email", "scenario@example.invalid")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / ".gitignore").write_text(".ahadiff/\n.codegraph/\ndist/\n", encoding="utf-8")
    for name, body in fixture["baseline"].items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8", newline="\n")
    if case in {"S02", "S04"}:
        _save(repo / "tsconfig.json", TS_CONFIG)
        _save(
            repo / "package.json",
            {"name": "growth-scenario-fixture", "private": True, "type": "module"},
        )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", fixture["commit"])
    base_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-c", fixture["branch"])
    next_root = run_dir / "fixture" / "next"
    for name, body in fixture["next"].items():
        target = next_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8", newline="\n")
    bundle = run_dir / "fixture" / "baseline.bundle"
    _git(repo, "bundle", "create", str(bundle), "--all")
    (run_dir / "artifacts").mkdir()
    for name in ("steps.jsonl", "events.jsonl"):
        (run_dir / name).write_text("", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "case_id": case,
        "release": release,
        "candidate_sha256": candidate_sha256,
        "supersedes_run_id": supersedes_run_id,
        "mode": "live",
        "started_at": _now(),
        "finished_at": None,
        "environment": {
            "os": platform.platform(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
        },
        "fixture": {
            "project": fixture["project"],
            "branch": fixture["branch"],
            "base_sha": base_sha,
            "bundle_sha256": _file_hash(bundle),
            "next_file_sha256": {name: _file_hash(next_root / name) for name in fixture["next"]},
        },
    }
    _save(run_dir / "manifest.json", manifest)
    _save(run_dir / "chain.json", {})
    result: dict[str, Any] = {
        "run_id": run_id,
        "case_id": case,
        "release": release,
        "mode": "live",
        "status": "NOT_RUN",
        "checks": {},
        "blockers": [],
    }
    _save(run_dir / "result.json", result)
    (run_dir / "report.md").write_text(
        f"# {case} 情境验收\n\n状态：**NOT_RUN**。Git 基线 `{base_sha}` 已创建。"
        "`fixture/next` 仅存待由 UI 捕获的源码变更；未预建卡片、答案或笔记。\n",
        encoding="utf-8",
    )
    return result


def _steps(run_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in (run_dir / "steps.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry: Any = json.loads(line)
            if not isinstance(entry, dict):
                raise ValueError("steps.jsonl 包含非对象记录")
            records.append(cast("dict[str, Any]", entry))
    return records


def _artifact_ok(run_dir: Path, item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    item = cast("dict[str, Any]", item)
    relative: Any = item.get("path")
    digest: Any = item.get("sha256")
    if not isinstance(relative, str) or not isinstance(digest, str):
        return False
    if not SHA256.fullmatch(digest):
        return False
    path = (run_dir / relative).resolve()
    return path.is_relative_to(run_dir.resolve()) and path.is_file() and _file_hash(path) == digest


def _aware_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def _step_ok(run_dir: Path, row: Any, started_at: datetime | None) -> bool:
    if not isinstance(row, dict):
        return False
    step = cast("dict[str, Any]", row)
    start = _aware_time(step.get("started_at"))
    finish = _aware_time(step.get("finished_at"))
    artifacts = step.get("artifacts")
    return bool(
        step.get("status") == "PASS"
        and start is not None and finish is not None
        and started_at is not None and started_at <= start <= finish
        and _uuid(step.get("device_id")) and _uuid(step.get("trace_id"))
        and isinstance(step.get("action"), str) and step["action"].strip()
        and isinstance(step.get("expected"), str) and step["expected"].strip()
        and isinstance(step.get("actual"), dict) and step["actual"]
        and isinstance(artifacts, list) and artifacts
        and all(_artifact_ok(run_dir, item) for item in cast("list[Any]", artifacts))
    )


def _proof_binding_ok(run_dir: Path, proof: dict[str, Any], manifest: dict[str, Any]) -> bool:
    """Require distinct live proof manifests bound to this run and raw evidence.

    This checks provenance and structure; semantic truth still needs the review
    described in the scenario acceptance contract.
    """
    proof_paths: set[Path] = set()
    proof_documents: list[dict[str, Any]] = []
    bundle_path = (run_dir / "fixture/baseline.bundle").resolve()
    for name in PROOF_NAMES:
        ref = proof.get(name)
        if not _artifact_ok(run_dir, ref):
            return False
        relative = ref["path"]
        proof_path = (run_dir / relative).resolve()
        if proof_path in proof_paths or proof_path == bundle_path:
            return False
        proof_paths.add(proof_path)
        try:
            document = _load(run_dir / relative)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return False
        if not isinstance(document, dict) or any(
            document.get(key) != expected
            for key, expected in (
                ("schema_version", 1),
                ("run_id", manifest["run_id"]),
                ("case_id", manifest["case_id"]),
                ("release", manifest["release"]),
                ("candidate_sha256", manifest["candidate_sha256"]),
                ("proof_type", name),
                ("mode", "live"),
                ("status", "PASS"),
            )
        ):
            return False
        observed = document.get("observed")
        if not isinstance(observed, dict) or not observed:
            return False
        proof_documents.append(document)
    for document in proof_documents:
        raw_refs = document.get("supporting_artifacts")
        if not isinstance(raw_refs, list) or not raw_refs:
            return False
        if any(
            not _artifact_ok(run_dir, item)
            or not (run_dir / item["path"]).resolve().is_relative_to(
                (run_dir / "artifacts").resolve()
            )
            or (run_dir / item["path"]).resolve() in proof_paths
            or (run_dir / item["path"]).resolve() == bundle_path
            for item in raw_refs
        ):
            return False
    return True


def _verify(run_dir: Path) -> dict[str, Any]:
    manifest = _load(run_dir / "manifest.json")
    result = _load(run_dir / "result.json")
    case = manifest["case_id"]
    if case not in STEPS or result.get("case_id") != case:
        raise ValueError("case_id 不匹配")
    if result.get("status") not in STATUSES:
        raise ValueError("非法结果状态")
    fixture = manifest["fixture"]
    bundle = run_dir / "fixture" / "baseline.bundle"
    bundle_hash_matches = _file_hash(bundle) == fixture.get("bundle_sha256")
    started_at = _aware_time(manifest.get("started_at"))
    finished_at = _aware_time(manifest.get("finished_at"))
    checks: dict[str, bool] = {
        "run_identity": manifest.get("run_id") == run_dir.name
        and result.get("run_id") == run_dir.name,
        "live_mode": manifest.get("mode") == "live" and result.get("mode") == "live",
        "same_release": result.get("release") == manifest.get("release"),
        "candidate_hash": bool(
            isinstance(manifest.get("candidate_sha256"), str)
            and SHA256.fullmatch(manifest["candidate_sha256"])
        ),
        "fixture_bundle": bundle_hash_matches,
        "git_base": bundle_hash_matches and _bundle_has_base(
            bundle, str(fixture.get("base_sha", ""))
        ),
        "started_at": started_at is not None,
    }
    records = _steps(run_dir)
    by_id = {row.get("step_id"): row for row in records}
    checks["unique_steps"] = len(by_id) == len(records)
    allowed_steps = {f"{case}-{number:02d}" for number in range(1, STEPS[case] + 1)}
    checks["known_steps"] = set(by_id) <= allowed_steps
    checks["ordered_steps"] = [row.get("step_id") for row in records] == [
        f"{case}-{number:02d}" for number in range(1, len(records) + 1)
    ]
    # Existing partial runs predate the record identity fields. A complete run
    # must carry them so its step records cannot be copied from another run.
    checks["step_identity"] = all(
        row.get("run_id") in (None, manifest["run_id"])
        and row.get("case_id") in (None, case)
        and row.get("schema_version") in (None, 1)
        for row in records
    ) and (len(records) < STEPS[case] or all(
        row.get("run_id") == manifest["run_id"]
        and row.get("case_id") == case
        and row.get("schema_version") == 1
        for row in records
    ))
    for number in range(1, STEPS[case] + 1):
        step_id = f"{case}-{number:02d}"
        row = by_id.get(step_id)
        checks[step_id] = _step_ok(run_dir, row, started_at)
    all_steps = all(checks[step_id] for step_id in allowed_steps)
    checks["finished_at"] = not all_steps or bool(
        finished_at is not None and started_at is not None
        and finished_at >= started_at
        and all(finished_at >= _aware_time(row["finished_at"])
                for row in records if row.get("step_id") in allowed_steps)
    )
    raw_proof = result.get("proof", {})
    proof: dict[str, Any] = cast("dict[str, Any]", raw_proof) if isinstance(raw_proof, dict) else {}
    for name in PROOF_NAMES:
        checks[name] = _artifact_ok(run_dir, proof.get(name))
    checks["proof_binding"] = not all_steps or _proof_binding_ok(run_dir, proof, manifest)
    chain = _load(run_dir / "chain.json")
    checks["chain_ids"] = all(
        bool(chain.get(name))
        for name in ("project_id", "snapshot_id", "task_id", "attempt_ids", "note_id")
    )
    checks["artifact_integrity"] = True
    for row in records:
        artifacts: Any = row.get("artifacts", [])
        if (
            not isinstance(artifacts, list)
            or not artifacts
            or not all(_artifact_ok(run_dir, item) for item in cast("list[Any]", artifacts))
        ):
            checks["artifact_integrity"] = False
    status = "PASS" if all(checks.values()) else "BLOCKED"
    if any(row.get("status") == "FAIL" for row in records):
        status = "FAIL"
    return {
        "run_id": manifest["run_id"],
        "case_id": case,
        "release": manifest["release"],
        "mode": manifest["mode"],
        "status": status,
        "checks": checks,
        "proof": proof,
        "blockers": [name for name, ok in checks.items() if not ok],
    }


def verify(root: Path, run_id: str) -> dict[str, Any]:
    run_dir = _run_dir(root, run_id)
    verified = _verify(run_dir)
    current = _load(run_dir / "result.json")
    # Never overwrite an earlier PASS/FAIL record in place.
    if current.get("status") in {"PASS", "FAIL"} and current != verified:
        raise ValueError("既有最终结果不可改写；请使用新的 run_id")
    _save(run_dir / "result.json", verified)
    prepared_report = run_dir / "report.md"
    report_text = prepared_report.read_text(encoding="utf-8")
    initial_status = "状态：**NOT_RUN**。"
    if initial_status in report_text:
        prepared_report.write_text(
            report_text.replace(initial_status,
                                f"状态：**{verified['status']}**。", 1),
            encoding="utf-8",
        )
    return verified


def run(root: Path, case: str, mode: str, run_id: str) -> dict[str, Any]:
    run_dir = _run_dir(root, run_id)
    manifest = _load(run_dir / "manifest.json")
    if manifest["case_id"] != case:
        raise ValueError("运行 case 与准备素材不匹配")
    if mode != "live":
        raise ValueError("发布验收只接受 live；回放请使用阶段情境脚本")
    result = _load(run_dir / "result.json")
    if result["status"] in {"PASS", "FAIL"}:
        raise ValueError("既有最终结果不可改写；请使用新的 run_id")
    # The CLI owns the gate. UI actions are recorded in steps.jsonl by the
    # platform driver; an absent driver leaves the run explicitly blocked.
    return verify(root, run_id)


def report(root: Path, release: str) -> dict[str, Any]:
    _run_dir(root.parent / "releases", release)
    run_manifests: dict[str, dict[str, Any]] = {}
    for path in root.glob("*/manifest.json"):
        manifest = _load(path)
        if manifest.get("release") != release:
            continue
        run_manifests[manifest["run_id"]] = manifest
    cases: dict[str, dict[str, Any]] = {}
    active_hashes: set[str | None] = set()
    superseded = {
        item["supersedes_run_id"]
        for item in run_manifests.values()
        if item.get("supersedes_run_id")
    }
    for run_id, manifest in run_manifests.items():
        old_id = manifest.get("supersedes_run_id")
        if old_id and (
            old_id not in run_manifests
            or run_manifests[old_id].get("case_id") != manifest["case_id"]
        ):
            raise ValueError("被替代运行缺失或 case 不一致")
        if run_id in superseded:
            continue
        result = _verify(_run_dir(root, run_id))
        case = result["case_id"]
        if case in cases:
            raise ValueError(f"同一发布候选有多个有效 {case} 运行；须明确失败重跑关系")
        cases[case] = result
        candidate_hash = manifest.get("candidate_sha256")
        active_hashes.add(candidate_hash if isinstance(candidate_hash, str) else None)
    status = (
        "PASS"
        if set(cases) == set(STEPS) and all(value["status"] == "PASS" for value in cases.values())
        else "BLOCKED"
    )
    if any(item["status"] == "FAIL" for item in cases.values()):
        status = "FAIL"
    if len(active_hashes) != 1 or None in active_hashes:
        status = "BLOCKED"
    summary = {
        "release": release,
        "status": status,
        "cases": cases,
        "missing_cases": sorted(set(STEPS) - set(cases)),
        "candidate_sha256": next(iter(active_hashes)) if len(active_hashes) == 1 else None,
    }
    release_dir = root.parent / "releases" / release
    release_dir.mkdir(parents=True, exist_ok=True)
    _save(release_dir / "result.json", summary)
    lines = [f"# 发布候选 {release} 情境报告", "", f"状态：**{status}**。", ""]
    for case in STEPS:
        item = cases.get(case)
        if item:
            recorded = len(_steps(_run_dir(root, item["run_id"])))
            lines.append(f"- {case}: {item['status']}（`{item['run_id']}`；"
                         f"已记录步骤 {recorded}/{STEPS[case]}）")
        else:
            lines.append(f"- {case}: NOT_RUN")
    lines.extend(
        ["", "只有四个情境在同一候选上经 live 完整取证并通过时，发布候选才可标 PASS。", ""]
    )
    (release_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="成长伴侣四情境验收")
    parser.add_argument("command", choices=("prepare", "run", "verify", "report"))
    parser.add_argument("--case", choices=tuple(STEPS))
    parser.add_argument("--run-id")
    parser.add_argument("--mode", default="live")
    parser.add_argument("--release")
    parser.add_argument("--supersedes-run-id")
    parser.add_argument("--candidate-sha256")
    parser.add_argument("--runs-root", type=Path, default=Path.cwd() / "acceptance" / "runs")
    args = parser.parse_args()
    if args.command in {"prepare", "run"} and not args.case:
        parser.error("prepare/run 必须指定 --case")
    if args.command in {"prepare", "run", "verify"} and not args.run_id:
        parser.error("prepare/run/verify 必须指定 --run-id")
    if args.command == "report" and not args.release:
        parser.error("report 必须指定 --release")
    try:
        if args.command == "prepare":
            output = prepare(
                args.runs_root,
                args.case,
                args.run_id,
                args.release or "r1-candidate-001",
                args.supersedes_run_id,
                args.candidate_sha256,
            )
        elif args.command == "run":
            output = run(args.runs_root, args.case, args.mode, args.run_id)
        elif args.command == "verify":
            output = verify(args.runs_root, args.run_id)
        else:
            output = report(args.runs_root, args.release)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        parser.exit(2, f"验收命令失败：{exc}\n")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if args.command in {"run", "verify", "report"} and output["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
