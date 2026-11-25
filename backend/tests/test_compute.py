"""计算引擎单元测试：覆盖 partition_calculator、merge_strategy、compaction_planner、
snapshot_differ、retention_checker、version_conflict_resolver、delta_reconciler。"""
from __future__ import annotations

import sys
sys.path.insert(0, '.')

from app.compute import (
    partition_calculator,
    merge_strategy,
    compaction_planner,
    snapshot_differ,
    retention_checker,
    version_conflict_resolver,
    delta_reconciler,
)


class TestPartitionCalculator:
    def test_small_row_count(self):
        result = partition_calculator(1000, ["dt"], 128.0)
        assert result["partition_count"] >= 1
        assert result["strategy"] in ("hash", "range")

    def test_large_row_count_hash(self):
        result = partition_calculator(100000, ["dt", "hour"], 128.0)
        assert result["partition_count"] > 1
        assert result["partition_keys"] == ["dt", "hour"]

    def test_target_size(self):
        result = partition_calculator(12800, ["dt"], 128.0)
        assert result["target_partition_size_mb"] == 128.0
        assert "estimated_rows_per_partition" in result


class TestMergeStrategy:
    def test_should_merge(self):
        result = merge_strategy([10, 20, 30, 5, 15], 256.0)
        assert result["should_merge"] is True

    def test_skip_merge_too_few_files(self):
        result = merge_strategy([10, 20], 256.0, min_file_count=3)
        assert result["should_merge"] is False

    def test_empty_files(self):
        result = merge_strategy([], 256.0)
        assert result["should_merge"] is False


class TestCompactionPlanner:
    def test_no_candidates(self):
        result = compaction_planner("test_table", [256 * 1024 * 1024, 512 * 1024 * 1024, 1024 * 1024 * 1024])
        assert result["plan"] is None
        assert "没有需要压缩的小文件" in result["reason"]

    def test_with_candidates(self):
        result = compaction_planner("test_table", [64 * 1024 * 1024, 32 * 1024 * 1024, 16 * 1024 * 1024, 8 * 1024 * 1024, 256 * 1024 * 1024, 512 * 1024 * 1024])
        assert result["candidate_file_count"] >= 1
        assert result["parallelism"] >= 1

    def test_parallelism_capped(self):
        result = compaction_planner("test_table", [8 * 1024 * 1024] * 20, max_parallelism=4)
        assert result["parallelism"] <= 4


class TestSnapshotDiffer:
    def test_no_changes(self):
        base = {"snapshot_id": "s1", "files": [{"path": "a.parquet", "checksum": "abc"}]}
        curr = {"snapshot_id": "s2", "files": [{"path": "a.parquet", "checksum": "abc"}]}
        result = snapshot_differ(base, curr)
        assert result["added_files"] == 0
        assert result["removed_files"] == 0
        assert result["modified_files"] == 0

    def test_added_and_removed(self):
        base = {"snapshot_id": "s1", "files": [
            {"path": "a.parquet", "checksum": "abc", "rows": 100}
        ]}
        curr = {"snapshot_id": "s2", "files": [
            {"path": "b.parquet", "checksum": "def", "rows": 200}
        ]}
        result = snapshot_differ(base, curr)
        # 文件数按实际检测到的差异计数
        assert result["added_files"] == 1
        assert result["removed_files"] == 1
        # 未提供证据：差异不能按零计，进入待核清单并保留行数口径，
        # 已证实行数保持 0，而不是把无法确认的差异吞掉。
        assert result["proven_diff_items"] == 0
        assert result["pending_diff_items"] == 2
        assert result["pending_rows"] == 300  # 新增 200 + 删除 100 均待证
        assert result["added_rows"] == 0
        assert result["removed_rows"] == 0

    def test_evidence_moves_diff_into_proven(self):
        base = {"snapshot_id": "s1", "files": [
            {"path": "a.parquet", "checksum": "abc", "rows": 100}
        ]}
        curr = {"snapshot_id": "s2", "files": [
            {"path": "b.parquet", "checksum": "def", "rows": 200}
        ]}
        result = snapshot_differ(base, curr, evidence={"b.parquet": {"type": "add"}})
        # 有证据的新增进入可证口径，无证据的删除仍待核
        assert result["proven_diff_items"] == 1
        assert result["pending_diff_items"] == 1
        assert result["added_rows"] == 200
        assert result["removed_rows"] == 0

    def test_modified(self):
        base = {"snapshot_id": "s1", "files": [
            {"path": "a.parquet", "checksum": "abc", "rows": 100}
        ]}
        curr = {"snapshot_id": "s2", "files": [
            {"path": "a.parquet", "checksum": "xyz", "rows": 150}
        ]}
        result = snapshot_differ(base, curr)
        assert result["modified_files"] == 1


class TestRetentionChecker:
    def test_recent_snapshots(self):
        snaps = [{"snapshot_id": f"s{i}", "created_at": "2024-11-20T00:00:00Z"} for i in range(5)]
        result = retention_checker(snaps, retention_days=30)
        assert result["valid_count"] == 0  # all expired vs now
        assert result["should_clean"] is False

    def test_mixed_snapshots(self):
        snaps = (
            [{"snapshot_id": f"s{i}", "created_at": "2023-01-01T00:00:00Z"} for i in range(3)] +
            [{"snapshot_id": f"s{i+3}", "created_at": "2024-11-20T00:00:00Z"} for i in range(2)]
        )
        result = retention_checker(snaps, retention_days=30)
        assert result["total_snapshots"] == 5
        assert result["valid_count"] >= 0


class TestVersionConflictResolver:
    def test_no_conflict(self):
        result = version_conflict_resolver(5, 5)
        assert result["conflict"] is False

    def test_lww(self):
        result = version_conflict_resolver(3, 7, strategy="last-write-wins")
        assert result["resolved_version"] == 7

    def test_base_wins(self):
        result = version_conflict_resolver(10, 5, strategy="base-wins")
        assert result["resolved_version"] == 10


class TestDeltaReconciler:
    def test_merge_with_overlap(self):
        stream = [{"id": 1, "val": "s1"}, {"id": 2, "val": "s2"}]
        batch = [{"id": 2, "val": "b2"}, {"id": 3, "val": "b3"}]
        result = delta_reconciler(stream, batch, dedup_key="id")
        assert result["duplicates_resolved"] == 1
        assert result["reconciled_total"] == 3
        assert result["checksum"]

    def test_no_overlap(self):
        stream = [{"id": 1, "val": "s1"}]
        batch = [{"id": 2, "val": "b2"}]
        result = delta_reconciler(stream, batch, dedup_key="id")
        assert result["duplicates_resolved"] == 0
        assert result["reconciled_total"] == 2

    def test_empty_streams(self):
        result = delta_reconciler([], [{"id": 1, "val": "a"}], dedup_key="id")
        assert result["stream_count"] == 0
        assert result["reconciled_total"] == 1