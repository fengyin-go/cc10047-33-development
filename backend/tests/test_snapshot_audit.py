"""快照差异审计测试：重复路径、待核清单、逐项/整张签核边界与签核幂等。

覆盖题目规则：
1. 同路径重复条目不被最后一条覆盖，与单路径清单区分；
2. 不同校验值的同路径条目不因排列顺序被判定变化（顺序无关）；
3. 无证据差异不按零计，进入快照差异待核清单；
4. 逐项边界：p8 可凭独立删除证据先签，p7 等待补证；
5. 整张边界：p7 未补证则整份 S13 冻结，p8 一并等待；
6. 同一比较只留一条签核结论，补证失败重试复用首份结果，签核次数不增加；
7. 后补证据不能把整张快照提前标为已确认；
8. 保留原文件顺序、未定项与已核范围，签核结果回写审计待办。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

import pytest

from app.compute import snapshot_differ
from app.snapshot_audit import (
    BOUNDARY_ITEM,
    BOUNDARY_SNAPSHOT,
    COMPARISON_CONFIRMED,
    COMPARISON_FROZEN,
    COMPARISON_PARTIAL,
    STATUS_PENDING,
    STATUS_SIGNED,
    STATUS_WAITING,
    snapshot_audit_store,
)

P7 = "data/p7/part-000.parquet"
P8 = "data/p8/part-001.parquet"


@pytest.fixture(autouse=True)
def fresh_s13():
    snapshot_audit_store.reset("S13")
    yield
    snapshot_audit_store.reset("S13")


class TestSnapshotDifferEngine:
    def test_duplicate_path_not_overwritten(self):
        base = {"snapshot_id": "b", "files": [
            {"path": P7, "checksum": "c0", "rows": 100},
        ]}
        curr = {"snapshot_id": "c", "files": [
            {"path": P7, "checksum": "c1", "rows": 110},
            {"path": P7, "checksum": "c2", "rows": 15},
        ]}
        result = snapshot_differ(base, curr)
        # 重复路径必须被识别，不能只剩最后一条 c2
        conflict = next(item for item in result["diff_items"] if item["path"] == P7)
        assert conflict["duplicate_path"] is True
        assert conflict["change_type"] == "duplicate_conflict"
        assert conflict["current_checksums"] == ["c1", "c2"]
        assert result["duplicate_conflict_files"] == 1

    def test_order_swap_does_not_change_result(self):
        base = {"snapshot_id": "b", "files": [
            {"path": P7, "checksum": "c1", "rows": 1},
            {"path": P7, "checksum": "c2", "rows": 2},
        ]}
        curr_a = {"snapshot_id": "c", "files": [
            {"path": P7, "checksum": "c2", "rows": 2},
            {"path": P7, "checksum": "c1", "rows": 1},
        ]}
        result = snapshot_differ(base, curr_a)
        # 仅交换顺序、校验值多重集合相同 => 无差异
        assert result["total_diff_items"] == 0
        assert result["diff_items"] == []

    def test_evidence_split_and_no_zero_counting(self):
        base = {"snapshot_id": "b", "files": [
            {"path": P8, "checksum": "d0", "rows": 80},
            {"path": "x", "checksum": "x0", "rows": 40},
        ]}
        curr = {"snapshot_id": "c", "files": [
            {"path": "x", "checksum": "x1", "rows": 60},
        ]}
        # 只有 P8 的删除有证据，x 的修改无证据
        result = snapshot_differ(base, curr, evidence={P8: {"type": "delete"}})
        assert result["proven_diff_items"] == 1
        assert result["pending_diff_items"] == 1
        pending = result["pending_items"][0]
        assert pending["path"] == "x"
        # 无证据差异不能按零计：pending_rows 保留其行数口径
        assert result["pending_rows"] == 60
        assert result["modified_rows"] == 0  # 未证实不计入已证口径
        assert result["removed_rows"] == 80

    def test_original_order_preserved(self):
        base = {"snapshot_id": "b", "files": [
            {"path": "a", "checksum": "1"},
            {"path": "b", "checksum": "2"},
            {"path": "c", "checksum": "3"},
        ]}
        curr = {"snapshot_id": "c", "files": [
            {"path": "a", "checksum": "9"},
            {"path": "b", "checksum": "2"},
            {"path": "c", "checksum": "8"},
        ]}
        result = snapshot_differ(base, curr)
        assert result["order_preserved"] == ["a", "c"]
        assert [item["order"] for item in result["diff_items"]] == [0, 1]


class TestAbnormalEntries:
    def test_read_abnormal_entries(self):
        payload = snapshot_audit_store.read_abnormal_entries("S13")
        assert payload["abnormal_count"] >= 1
        paths = {(item["path"], tuple(item["reasons"])) for item in payload["items"]}
        assert any(path == P7 and "duplicate_path" in reasons for path, reasons in paths)
        # 证据缺失的 orphan 校验值条目必须被读出
        missing = [
            item for item in payload["items"]
            if item["checksum"] == "p7-orphan-checksum" and "missing_evidence" in item["reasons"]
        ]
        assert missing, "p7 证据缺失条目未被异常条目读取发现"

    def test_pending_rows_not_counted_as_zero(self):
        payload = snapshot_audit_store.build_checklist("S13")
        # p7 重复冲突待证：110 + 15 行必须保留在待证口径，不能按零计
        assert payload["pending_diff_items"] == 1
        assert payload["pending_rows"] == 125
        assert payload["proven_diff_items"] == 1


class TestItemBoundary:
    def test_p8_signed_first_p7_waits(self):
        snapshot_audit_store.build_checklist("S13")
        audit = snapshot_audit_store.open_audit("S13", BOUNDARY_ITEM)
        assert audit["boundary"] == BOUNDARY_ITEM

        checklist = snapshot_audit_store.get_checklist("S13")["checklist"]
        p7_row = next(row for row in checklist if row["path"] == P7)
        p8_row = next(row for row in checklist if row["path"] == P8)
        assert p7_row["evidence_status"] == STATUS_PENDING
        assert p8_row["evidence_status"] == "proven"

        # p8 凭独立删除证据先签
        result = snapshot_audit_store.sign_off("S13", item_key=p8_row["key"])
        assert result["ok"] is True
        assert result["signoff_count"] == 1
        comparison = snapshot_audit_store.get_comparison("S13")
        assert comparison["status"] == COMPARISON_PARTIAL
        assert p8_row["key"] in comparison["audited_item_keys"]
        assert p7_row["key"] in comparison["waiting_item_keys"]

        # p7 无证据不能签，也不能按零计
        rejected = snapshot_audit_store.sign_off("S13", item_key=p7_row["key"])
        assert rejected["ok"] is False
        assert rejected["signoff_count"] == 1  # 未签成功，次数不增加
        still = snapshot_audit_store.get_comparison("S13")
        assert still["status"] == COMPARISON_PARTIAL

    def test_supplement_then_reopen_and_confirm(self):
        snapshot_audit_store.open_audit("S13", BOUNDARY_ITEM)
        checklist = snapshot_audit_store.get_checklist("S13")["checklist"]
        p7_key = next(row["key"] for row in checklist if row["path"] == P7)
        p8_key = next(row["key"] for row in checklist if row["path"] == P8)
        snapshot_audit_store.sign_off("S13", item_key=p8_key)

        # 补全 p7 缺失证据
        ok = snapshot_audit_store.supplement_evidence(
            "S13", P7, "p7-orphan-checksum", "补偿提交 commit#2009", succeed=True
        )
        assert ok["ok"] is True
        reopened = snapshot_audit_store.reopen_checklist("S13")
        p7_row = next(row for row in reopened["checklist"] if row["path"] == P7)
        assert p7_row["evidence_status"] == "proven"
        # 重开清单不改变既有签核次数
        assert reopened["signoff_count"] == 1

        # 补签 p7 后整张才确认
        signed = snapshot_audit_store.sign_off("S13", item_key=p7_key)
        assert signed["ok"] is True
        final = snapshot_audit_store.get_comparison("S13")
        assert final["status"] == COMPARISON_CONFIRMED
        assert final["signoff_count"] == 2

    def test_failed_supplement_reuses_first_conclusion(self):
        snapshot_audit_store.open_audit("S13", BOUNDARY_ITEM)
        checklist = snapshot_audit_store.get_checklist("S13")["checklist"]
        p8_key = next(row["key"] for row in checklist if row["path"] == P8)
        first = snapshot_audit_store.sign_off("S13", item_key=p8_key)
        assert first["ok"] is True
        first_attempt = first["attempt"]

        # 补证失败
        failed = snapshot_audit_store.supplement_evidence(
            "S13", P7, "p7-orphan-checksum", "伪造来源", succeed=False
        )
        assert failed["ok"] is False
        assert failed["signoff_count"] == 1

        # 再次签 p8：复用首份结论，签核次数不增加
        retry = snapshot_audit_store.sign_off("S13", item_key=p8_key)
        assert retry["ok"] is False
        assert retry["reused"] is True
        assert retry["signoff_count"] == 1
        comparison = snapshot_audit_store.get_comparison("S13")
        assert comparison["conclusion_attempt"] == first_attempt
        assert comparison["attempt_count"] > first_attempt


class TestSnapshotBoundary:
    def test_whole_snapshot_frozen_p8_waits(self):
        snapshot_audit_store.open_audit("S13", BOUNDARY_SNAPSHOT)
        comparison = snapshot_audit_store.get_comparison("S13")
        assert comparison["status"] == COMPARISON_FROZEN

        checklist = {row["path"]: row for row in comparison["checklist"]}
        # p8 虽然有独立删除证据，整张冻结下也必须等待
        assert checklist[P8]["signoff_status"] == STATUS_WAITING
        assert checklist[P7]["signoff_status"] == STATUS_WAITING

        # 尝试整张签核 => 拒绝，且不产生签核次数
        result = snapshot_audit_store.sign_off("S13")
        assert result["ok"] is False
        assert result["signoff_count"] == 0
        assert snapshot_audit_store.get_comparison("S13")["status"] == COMPARISON_FROZEN

    def test_frozen_snapshot_confirmed_only_after_p7_completed(self):
        snapshot_audit_store.open_audit("S13", BOUNDARY_SNAPSHOT)
        # 补证 p7
        snapshot_audit_store.supplement_evidence(
            "S13", P7, "p7-orphan-checksum", "补偿提交 commit#2009", succeed=True
        )
        # 后补证据本身不能提前确认整张快照
        mid = snapshot_audit_store.get_comparison("S13")
        assert mid["status"] == COMPARISON_FROZEN
        assert mid["signoff_count"] == 0

        reopened = snapshot_audit_store.reopen_checklist("S13")
        assert reopened["pending_diff_items"] == 0
        signed = snapshot_audit_store.sign_off("S13")
        assert signed["ok"] is True
        final = snapshot_audit_store.get_comparison("S13")
        assert final["status"] == COMPARISON_CONFIRMED
        assert final["signoff_count"] == 1
        assert len(final["audited_item_keys"]) == len(final["checklist"])


class TestOrderIndependence:
    def test_swap_p7_order_then_audit(self):
        before = snapshot_audit_store.build_checklist("S13")
        before_p7 = next(row for row in before["checklist"] if row["path"] == P7)
        assert before_p7["current_checksums"] == [
            "p7-new-checksum", "p7-orphan-checksum",
        ]

        swapped = snapshot_audit_store.reorder_duplicate_entries("S13", P7)
        assert swapped["current_order"] == [
            "p7-orphan-checksum", "p7-new-checksum",
        ]

        after = snapshot_audit_store.build_checklist("S13")
        after_p7 = next(row for row in after["checklist"] if row["path"] == P7)
        # 顺序交换后仍是同一条待核差异、仍因证据缺失而待证
        assert after_p7["key"] == before_p7["key"]
        assert after_p7["evidence_status"] == STATUS_PENDING
        assert after_p7["duplicate_path"] is True
        assert after["proven_diff_items"] == before["proven_diff_items"]
        assert after["pending_diff_items"] == before["pending_diff_items"]

        # 交换后逐项审计：p8 依旧可先完成
        snapshot_audit_store.open_audit("S13", BOUNDARY_ITEM)
        p8_key = next(
            row["key"] for row in
            snapshot_audit_store.get_checklist("S13")["checklist"]
            if row["path"] == P8
        )
        signed = snapshot_audit_store.sign_off("S13", item_key=p8_key)
        assert signed["ok"] is True
        p8_todo = next(
            todo for todo in
            snapshot_audit_store.get_comparison("S13")["audit_todo"]
            if todo["path"] == P8
        )
        assert p8_todo["signoff_status"] == STATUS_SIGNED


class TestAuditTodoWriteback:
    def test_signoff_written_back_and_pending_evidence_collected(self):
        snapshot_audit_store.open_audit("S13", BOUNDARY_ITEM)
        checklist = snapshot_audit_store.get_checklist("S13")["checklist"]
        p8_key = next(row["key"] for row in checklist if row["path"] == P8)
        snapshot_audit_store.sign_off("S13", item_key=p8_key)

        comparison = snapshot_audit_store.get_comparison("S13")
        todo = next(item for item in comparison["audit_todo"] if item["item_key"] == p8_key)
        assert todo["signoff_status"] == STATUS_SIGNED
        assert todo["conclusion"] == "confirmed"

        # 未定范围的签核证据汇总到待核清单数据
        p7_row = next(row for row in comparison["checklist"] if row["path"] == P7)
        assert p7_row.get("evidence_summary") is not None
        assert p7_row["evidence_summary"]["evidence_status"] == STATUS_PENDING
        assert "p7-orphan-checksum" not in p7_row["evidence_summary"]["covered_checksums"]

    def test_boundary_cannot_switch_midway(self):
        snapshot_audit_store.open_audit("S13", BOUNDARY_ITEM)
        with pytest.raises(ValueError):
            snapshot_audit_store.open_audit("S13", BOUNDARY_SNAPSHOT)
