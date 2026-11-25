"""快照差异审计 API 端到端测试：用真实路由走一遍题目中的 S13 操作序列。"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.snapshot_audit import snapshot_audit_store

client = TestClient(app)

P7 = "data/p7/part-000.parquet"
P8 = "data/p8/part-001.parquet"
BASE = "/api/snapshot-audit/comparisons/S13"


@pytest.fixture(autouse=True)
def reset_s13():
    snapshot_audit_store.reset("S13")
    yield
    snapshot_audit_store.reset("S13")


class TestSnapshotAuditApi:
    def test_full_item_boundary_flow(self):
        # 1. 异常条目读取：p7 重复路径 + orphan 证据缺失
        resp = client.get(f"{BASE}/abnormal-entries")
        assert resp.status_code == 200
        abnormal = resp.json()
        reasons = {(item["path"], item["checksum"]): item["reasons"] for item in abnormal["items"]}
        assert "duplicate_path" in reasons[(P7, "p7-new-checksum")]
        assert "missing_evidence" in reasons[(P7, "p7-orphan-checksum")]

        # 2. 交换 p7 两条记录顺序后再生成清单，差异判定不变
        swap = client.post(f"{BASE}/reorder", params={"path": P7})
        assert swap.status_code == 200
        assert swap.json()["current_order"] == ["p7-orphan-checksum", "p7-new-checksum"]

        checklist = client.post(f"{BASE}/checklist").json()
        assert checklist["proven_diff_items"] == 1   # 仅 p8
        assert checklist["pending_diff_items"] == 1  # p7 重复冲突待证
        # 原文件顺序保留
        assert checklist["order_preserved"] == [P7, P8]

        # 3. 发起审计，记录逐项签核边界
        audit = client.post(f"{BASE}/audit", json={"boundary": "item"})
        assert audit.status_code == 200
        assert audit.json()["boundary"] == "item"
        # 边界不能中途切换
        conflict = client.post(f"{BASE}/audit", json={"boundary": "snapshot"})
        assert conflict.status_code == 409

        p7_key = next(row["key"] for row in audit.json()["checklist"] if row["path"] == P7)
        p8_key = next(row["key"] for row in audit.json()["checklist"] if row["path"] == P8)

        # 4. p8 凭独立删除证据先完成签核，p7 保持等待
        signed = client.post(f"{BASE}/signoff", json={"item_key": p8_key})
        assert signed.json()["ok"] is True
        assert signed.json()["signoff_count"] == 1

        # 5. p7 无证据签核被拒，不能按零计
        rejected = client.post(f"{BASE}/signoff", json={"item_key": p7_key})
        assert rejected.json()["ok"] is False
        assert rejected.json()["signoff_count"] == 1

        # 6. 补证失败：重试 p8 复用首份结论，签核次数不增加
        failed = client.post(f"{BASE}/evidence", json={
            "path": P7, "checksum": "p7-orphan-checksum",
            "evidence_detail": "来源不明", "succeed": False,
        })
        assert failed.json()["ok"] is False
        retry = client.post(f"{BASE}/signoff", json={"item_key": p8_key})
        assert retry.json()["reused"] is True
        assert retry.json()["signoff_count"] == 1

        # 7. 补全 p7 后重开差异清单，已核范围保留、次数不变
        ok = client.post(f"{BASE}/evidence", json={
            "path": P7, "checksum": "p7-orphan-checksum",
            "evidence_detail": "补偿提交 commit#2009", "succeed": True,
        })
        assert ok.json()["ok"] is True
        reopened = client.post(f"{BASE}/reopen").json()
        assert reopened["pending_diff_items"] == 0
        assert reopened["signoff_count"] == 1
        p8_row = next(row for row in reopened["checklist"] if row["path"] == P8)
        assert p8_row["signoff_status"] == "signed"  # 已核范围未被重开冲掉

        # 8. 补签 p7，整张才确认
        final_sign = client.post(f"{BASE}/signoff", json={"item_key": p7_key})
        assert final_sign.json()["ok"] is True
        comparison = client.get(BASE).json()
        assert comparison["status"] == "confirmed"
        assert comparison["signoff_count"] == 2
        assert comparison["conclusion"] == "confirmed"

        # 9. 审计待办回写
        todo_p8 = next(item for item in comparison["audit_todo"] if item["path"] == P8)
        assert todo_p8["signoff_status"] == "signed"

    def test_snapshot_boundary_freezes_p8(self):
        # 整张边界：p7 未补证 => S13 冻结，p8 一并等待
        audit = client.post(f"{BASE}/audit", json={"boundary": "snapshot"}).json()
        assert audit["status"] == "frozen"
        rows = {row["path"]: row for row in audit["checklist"]}
        assert rows[P8]["signoff_status"] == "waiting"
        assert rows[P7]["signoff_status"] == "waiting"

        blocked = client.post(f"{BASE}/signoff", json={})
        assert blocked.json()["ok"] is False
        assert blocked.json()["signoff_count"] == 0

        # 后补证据不能提前确认整张快照
        client.post(f"{BASE}/evidence", json={
            "path": P7, "checksum": "p7-orphan-checksum",
            "evidence_detail": "补偿提交 commit#2009", "succeed": True,
        })
        assert client.get(BASE).json()["status"] == "frozen"

        # 重开清单后整张签核才能通过，且只有一条签核结论
        reopened = client.post(f"{BASE}/reopen").json()
        assert reopened["pending_diff_items"] == 0
        signed = client.post(f"{BASE}/signoff", json={}).json()
        assert signed["ok"] is True
        assert signed["signoff_count"] == 1
        assert client.get(BASE).json()["status"] == "confirmed"

    def test_order_swap_is_diff_neutral(self):
        first = client.post(f"{BASE}/checklist").json()
        client.post(f"{BASE}/reorder", params={"path": P7})
        second = client.post(f"{BASE}/checklist").json()
        # 同路径条目交换顺序：待核项数量、类型、证据状态完全一致
        assert first["proven_diff_items"] == second["proven_diff_items"]
        assert first["pending_diff_items"] == second["pending_diff_items"]
        first_keys = [(row["key"], row["evidence_status"]) for row in first["checklist"]]
        second_keys = [(row["key"], row["evidence_status"]) for row in second["checklist"]]
        assert first_keys == second_keys
