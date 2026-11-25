"""实时流数据湖业务计算引擎：分区、合并、压缩、快照差异、保留策略、
版本冲突、增量协调等核心算法实现。
"""
from __future__ import annotations

import hashlib
import math
from typing import Any


# ------------------------------------------------------------------
# partition_calculator
# ------------------------------------------------------------------

def partition_calculator(
    rows: int, partition_keys: list[str], target_size_mb: float = 128.0,
) -> dict[str, Any]:
    """根据行数和分区键计算最佳分区策略。"""
    partitions = max(1, math.ceil(rows * 0.25 / target_size_mb))
    return {
        "partition_count": partitions,
        "partition_keys": partition_keys,
        "strategy": "hash" if partitions > 4 else "range",
        "estimated_rows_per_partition": rows // partitions,
        "target_partition_size_mb": target_size_mb,
    }


# ------------------------------------------------------------------
# merge_strategy
# ------------------------------------------------------------------

def merge_strategy(
    small_files: list[int],
    target_file_size_mb: float = 256.0,
    min_file_count: int = 3,
) -> dict[str, Any]:
    """计算小文件合并策略：按目标文件大小将多个小文件合为一个。"""
    if not small_files or len(small_files) < min_file_count:
        return {"should_merge": False, "reason": "小文件数量不足，跳过合并"}
    total_size = sum(small_files)
    groups = max(1, math.ceil(total_size / target_file_size_mb))
    return {
        "should_merge": True,
        "file_count": len(small_files),
        "total_size_mb": total_size,
        "target_groups": groups,
        "files_per_group": len(small_files) // groups,
        "estimated_output_mb": target_file_size_mb,
    }


# ------------------------------------------------------------------
# compaction_planner
# ------------------------------------------------------------------

def compaction_planner(
    table_path: str,
    file_sizes: list[int],
    target_file_size: int = 134217728,  # 128MB
    max_parallelism: int = 4,
) -> dict[str, Any]:
    """规划压缩作业：输入文件大小列表，输出分批压缩计划。"""
    candidates = [i for i, sz in enumerate(file_sizes) if sz < target_file_size // 4]
    if not candidates:
        return {
            "table": table_path,
            "plan": None,
            "candidate_file_count": 0,
            "batches": 0,
            "files_per_batch": 0,
            "parallelism": 0,
            "target_file_size_bytes": target_file_size,
            "estimated_duration_seconds": 0,
            "reason": "没有需要压缩的小文件",
        }
    batches = math.ceil(len(candidates) / max_parallelism)
    plan = {
        "candidate_file_count": len(candidates),
        "batches": batches,
        "files_per_batch": len(candidates) // batches if batches else 0,
        "parallelism": min(max_parallelism, len(candidates)),
        "target_file_size_bytes": target_file_size,
        "estimated_duration_seconds": len(candidates) * 15,
    }
    return {
        "table": table_path,
        "plan": plan,
        **plan,
    }


# ------------------------------------------------------------------
# snapshot_differ
# ------------------------------------------------------------------

def _index_files(files: list[dict[str, Any]]) -> dict[Any, list[dict[str, Any]]]:
    """按 path 分组但不覆盖同路径重复条目：每个路径保留一个有序条目列表。

    旧实现用 ``{f["path"]: f}`` 建索引，快照内同路径多份条目会被最后一条
    静默覆盖，审计详情里无法确认的差异也随之丢失，这里改为分组列表。
    """
    grouped: dict[Any, list[dict[str, Any]]] = {}
    for index, file_entry in enumerate(files):
        entry = dict(file_entry)
        entry.setdefault("_order", index)
        grouped.setdefault(file_entry.get("path"), []).append(entry)
    return grouped


def _checksum_multiset(entries: list[dict[str, Any]]) -> dict[Any, int]:
    """同路径条目按校验值计数：只看校验值集合，不看排列顺序。"""
    counts: dict[Any, int] = {}
    for entry in entries:
        checksum = entry.get("checksum")
        counts[checksum] = counts.get(checksum, 0) + 1
    return counts


def _make_diff_item(
    change_type: str,
    path: Any,
    base_entries: list[dict[str, Any]],
    curr_entries: list[dict[str, Any]],
    order: int,
) -> dict[str, Any]:
    """构造一条差异待核项，保留原文件顺序与同路径重复条目的全部校验值。"""
    base_checksums = [entry.get("checksum") for entry in base_entries]
    curr_checksums = [entry.get("checksum") for entry in curr_entries]
    duplicate = len(base_entries) > 1 or len(curr_entries) > 1
    return {
        "key": f"{change_type}:{path}",
        "change_type": change_type,  # added / removed / modified / duplicate_conflict
        "path": path,
        "order": order,
        "base_entries": [
            {key: value for key, value in entry.items() if not key.startswith("_")}
            for entry in base_entries
        ],
        "current_entries": [
            {key: value for key, value in entry.items() if not key.startswith("_")}
            for entry in curr_entries
        ],
        "base_checksums": base_checksums,
        "current_checksums": curr_checksums,
        "duplicate_path": duplicate,
        "base_rows": sum(int(entry.get("rows", 0) or 0) for entry in base_entries),
        "current_rows": sum(int(entry.get("rows", 0) or 0) for entry in curr_entries),
    }


def snapshot_differ(
    base_snapshot: dict[str, Any],
    current_snapshot: dict[str, Any],
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """比较两个快照的文件清单，输出新增/删除/修改/重复路径冲突的待核清单。

    与旧实现的区别：

    * 快照内同路径重复条目不互相覆盖，重复路径单独标记 ``duplicate_path``；
    * 同路径条目按校验值多重集合比较，交换排列顺序不会被判成变化；
    * 差异拆成 ``proven_items``（有证据）与 ``pending_items``（证据缺失，
      即快照差异待核清单），没有证据的差异不会被按零计，也不会进入已证实统计；
    * 每条差异保留其在原清单中的顺序（``order``），审计工作台按原顺序展示。
    """
    evidence = evidence or {}
    base_files = list(base_snapshot.get("files", []))
    curr_files = list(current_snapshot.get("files", []))
    base_grouped = _index_files(base_files)
    curr_grouped = _index_files(curr_files)

    diff_items: list[dict[str, Any]] = []
    order_counter = 0
    all_paths: list[Any] = []
    for path in [entry.get("path") for entry in base_files] + [
        entry.get("path") for entry in curr_files
    ]:
        if path not in all_paths:
            all_paths.append(path)

    for path in all_paths:
        base_entries = base_grouped.get(path, [])
        curr_entries = curr_grouped.get(path, [])
        base_multiset = _checksum_multiset(base_entries)
        curr_multiset = _checksum_multiset(curr_entries)

        if base_multiset == curr_multiset:
            # 校验值多重集合一致：即便同路径条目排列顺序不同，也不构成差异。
            continue

        if not base_entries:
            change_type = "added"
        elif not curr_entries:
            change_type = "removed"
        elif len(base_entries) > 1 or len(curr_entries) > 1:
            # 同路径存在重复条目且校验值对不平：重复路径与有效变化冲突，
            # 不能用最后一条覆盖，需要人工补证后逐项核对。
            change_type = "duplicate_conflict"
        else:
            change_type = "modified"

        diff_items.append(
            _make_diff_item(change_type, path, base_entries, curr_entries, order_counter)
        )
        order_counter += 1

    proven_items: list[dict[str, Any]] = []
    pending_items: list[dict[str, Any]] = []
    duplicate_paths: list[dict[str, Any]] = []
    for item in diff_items:
        path = item["path"]
        item_evidence = evidence.get(path) if isinstance(evidence, dict) else None
        # 证据若声明了 covered_checksums，则必须覆盖该差异涉及的全部校验值；
        # 部分覆盖（如重复路径只有一条有证据）仍判定为证据缺失。
        if item_evidence and isinstance(item_evidence.get("covered_checksums"), list):
            required = (
                item["base_checksums"] if item["change_type"] == "removed"
                else item["current_checksums"]
            )
            if any(checksum not in item_evidence["covered_checksums"] for checksum in required):
                item_evidence = None
        item["evidence"] = item_evidence
        item["evidence_status"] = "proven" if item_evidence else "pending"
        if item_evidence:
            proven_items.append(item)
        else:
            pending_items.append(item)
        if item["duplicate_path"]:
            duplicate_paths.append({
                "path": path,
                "base_checksums": item["base_checksums"],
                "current_checksums": item["current_checksums"],
                "change_type": item["change_type"],
                "order": item["order"],
            })

    # 已证实行数只统计有证据的差异；待证差异不按零计，单独给出 pending 口径，
    # 避免审计详情把无法确认的差异吞掉。
    proven_added_rows = sum(
        item["current_rows"] for item in proven_items if item["change_type"] == "added"
    )
    proven_removed_rows = sum(
        item["base_rows"] for item in proven_items if item["change_type"] == "removed"
    )
    proven_modified_rows = sum(
        item["current_rows"] for item in proven_items
        if item["change_type"] in ("modified", "duplicate_conflict")
    )
    pending_rows = sum(
        item["current_rows"] if item["change_type"] != "removed" else item["base_rows"]
        for item in pending_items
    )

    type_counts = {
        change_type: sum(1 for item in diff_items if item["change_type"] == change_type)
        for change_type in ("added", "removed", "modified", "duplicate_conflict")
    }

    return {
        "base_snapshot_id": base_snapshot.get("snapshot_id"),
        "current_snapshot_id": current_snapshot.get("snapshot_id"),
        # 文件数口径：按实际检测到的差异计数（含待证项），重复路径不再被覆盖。
        "added_files": type_counts["added"],
        "removed_files": type_counts["removed"],
        "modified_files": type_counts["modified"],
        "duplicate_conflict_files": type_counts["duplicate_conflict"],
        # 行数控口径：只累加已证实差异；未证实差异不按零计，走 pending_rows。
        "added_rows": proven_added_rows,
        "removed_rows": proven_removed_rows,
        "modified_rows": proven_modified_rows,
        "net_change_rows": proven_added_rows - proven_removed_rows,
        # 新口径：可证差异交给审计，无证据差异进入待核清单。
        "total_diff_items": len(diff_items),
        "proven_diff_items": len(proven_items),
        "pending_diff_items": len(pending_items),
        "pending_rows": pending_rows,
        "duplicate_paths": duplicate_paths,
        "proven_items": proven_items,
        "pending_items": pending_items,
        "diff_items": diff_items,
        "order_preserved": [item["path"] for item in diff_items],
    }


# ------------------------------------------------------------------
# retention_checker
# ------------------------------------------------------------------

def retention_checker(
    snapshots: list[dict[str, Any]],
    retention_days: int = 30,
    min_versions: int = 5,
) -> dict[str, Any]:
    """检查快照保留策略：标记可清理的过期快照。"""
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    expired = []
    valid = []

    for snap in snapshots:
        ts = str(snap.get("created_at", ""))
        # Strip timezone to make comparison naive
        for suffix in ("Z", "+00:00", "+0000"):
            ts = ts.replace(suffix, "")
        ts = ts.split("+")[0]
        try:
            created = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            created = cutoff + timedelta(days=1)
        if created.replace(tzinfo=None) < cutoff.replace(tzinfo=None):
            expired.append(snap)
        else:
            valid.append(snap)

    can_clean = max(0, len(expired) - min_versions)
    return {
        "total_snapshots": len(snapshots),
        "expired_count": len(expired),
        "valid_count": len(valid),
        "min_versions_required": min_versions,
        "retention_days": retention_days,
        "can_clean_count": can_clean,
        "should_clean": can_clean > 0,
    }


# ------------------------------------------------------------------
# version_conflict_resolver
# ------------------------------------------------------------------

def version_conflict_resolver(
    base_version: int,
    incoming_version: int,
    strategy: str = "last-write-wins",
    conflict_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """解决版本冲突：支持 LWW、三路合并和手动覆盖策略。"""
    if base_version == incoming_version:
        return {
            "conflict": False,
            "resolved_version": base_version,
            "strategy": strategy,
        }

    if strategy == "last-write-wins":
        winner = max(base_version, incoming_version)
    elif strategy == "base-wins":
        winner = base_version
    elif strategy == "incoming-wins":
        winner = incoming_version
    elif strategy == "manual" and conflict_data:
        winner = conflict_data.get("preferred_version", base_version)
    else:
        winner = base_version

    return {
        "conflict": base_version != incoming_version,
        "base_version": base_version,
        "incoming_version": incoming_version,
        "resolved_version": winner,
        "strategy": strategy,
        "resolution": "resolved" if strategy != "manual" else "needs_review",
    }


# ------------------------------------------------------------------
# delta_reconciler
# ------------------------------------------------------------------

def delta_reconciler(
    stream_events: list[dict[str, Any]],
    batch_events: list[dict[str, Any]],
    dedup_key: str = "id",
) -> dict[str, Any]:
    """协调流批数据：对流事件和批事件进行去重合并，输出最终写入集。"""
    # Build dedup index from batch (batch wins for same key)
    batch_index: dict[str, dict[str, Any]] = {}
    for event in batch_events:
        key = str(event.get(dedup_key, ""))
        if key:
            batch_index[key] = event

    reconciled = []
    duplicates = 0
    stream_only = 0

    for event in stream_events:
        key = str(event.get(dedup_key, ""))
        if key and key in batch_index:
            reconciled.append(event)  # batch wins, but keep source trace
            duplicates += 1
        else:
            reconciled.append(event)
            stream_only += 1

    # Append batch-only events
    existing_keys = {str(e.get(dedup_key, "")) for e in stream_events}
    batch_only = 0
    for key, event in batch_index.items():
        if key not in existing_keys:
            reconciled.append(event)
            batch_only += 1

    total = len(reconciled)
    checksum = _compute_checksum(str(reconciled))

    return {
        "stream_count": len(stream_events),
        "batch_count": len(batch_events),
        "reconciled_total": total,
        "duplicates_resolved": duplicates,
        "stream_only": stream_only,
        "batch_only": batch_only,
        "dedup_key": dedup_key,
        "checksum": checksum,
    }


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

def _compute_checksum(data: str) -> str:
    return hashlib.md5(data.encode()).hexdigest()[:12]