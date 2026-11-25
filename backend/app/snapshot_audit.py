"""快照差异审计领域：异常条目读取、快照差异待核清单、逐项/整张签核边界。

设计要点（对应审计规则）：

* 快照内同路径重复条目与完整单路径清单分开存放，比较时不做最后一条覆盖；
* 同路径条目按校验值多重集合比较，排列顺序不影响差异判定；
* 差异拆成「可证差异」（交给审计）与「待核清单」（证据缺失，不能按零计）；
* 签核支持两种边界：逐项（item，已证实条目可单独签核，其余等待补证）和
  整张（snapshot，任一条目待证则整份 S13 冻结，p8 也一并等待）；
* 同一比较只保留一条签核结论，补证失败重试复用首份结论，后补证据不能把
  整张快照提前标为已确认。
"""
from __future__ import annotations

import threading
from copy import deepcopy
from typing import Any

from app.compute import snapshot_differ

# 签核边界
BOUNDARY_ITEM = "item"
BOUNDARY_SNAPSHOT = "snapshot"

# 差异条目的签核状态
STATUS_PROVEN = "proven"          # 有证据、可交审计
STATUS_PENDING = "pending"        # 证据缺失，进入快照差异待核清单
STATUS_SIGNED = "signed"          # 已签核（结论已落）
STATUS_WAITING = "waiting"        # 整张冻结下等待补证
STATUS_REJECTED = "rejected"      # 补证失败，保留首份结论

# 比较/快照整体签核状态
COMPARISON_OPEN = "open"
COMPARISON_PARTIAL = "partially_signed"
COMPARISON_FROZEN = "frozen"
COMPARISON_CONFIRMED = "confirmed"


def _file(path: str, checksum: str, rows: int = 10, evidence: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": path, "checksum": checksum, "rows": rows}
    if evidence:
        entry["evidence"] = evidence
    return entry


# ------------------------------------------------------------------
# 场景数据：S13。
# p7 在当前快照里有两份同路径、不同校验值的条目，其中一条证据缺失；
# p8 在基线快照存在、当前快照消失，且带有独立删除证据。
# 重复路径冲突与有效删除同时出现，用于检验两种签核边界。
# ------------------------------------------------------------------
def _build_s13() -> dict[str, Any]:
    p7 = "data/p7/part-000.parquet"
    p8 = "data/p8/part-001.parquet"
    p9 = "data/p9/part-002.parquet"

    base_files = [
        _file(p7, "p7-base-checksum", rows=100, evidence="基线清单 manifest#1001"),
        _file(p8, "p8-checksum", rows=80, evidence="基线清单 manifest#1001"),
        _file(p9, "p9-checksum", rows=50, evidence="基线清单 manifest#1001"),
    ]
    # 当前快照：p7 两条同路径不同校验值（一条有证据、一条证据缺失），
    # p8 被删除（删除证据独立挂在删除事件上），p9 保持不变。
    current_files = [
        _file(p7, "p7-new-checksum", rows=110, evidence="写入提交 commit#2002"),
        _file(p7, "p7-orphan-checksum", rows=15),  # 重复路径，证据缺失
        _file(p9, "p9-checksum", rows=50, evidence="写入提交 commit#2002"),
    ]
    # 证据表：按路径登记当前可采信的证据。p7 只有一条有证据，另一条缺失；
    # p8 的删除证据独立存在，不依赖 p7 的重复路径冲突。
    evidence_registry: dict[str, dict[str, Any]] = {
        p7: {
            "path": p7,
            "type": "modify",
            "available": True,
            "detail": "写入提交 commit#2002 仅覆盖 p7-new-checksum；p7-orphan-checksum 无来源证据",
            "covered_checksums": ["p7-new-checksum"],
            "missing_checksums": ["p7-orphan-checksum"],
        },
        p8: {
            "path": p8,
            "type": "delete",
            "available": True,
            "detail": "删除事务 txn#3003 登记 p8 删除，独立于 p7 重复路径冲突",
            "covered_checksums": ["p8-checksum"],
            "missing_checksums": [],
        },
    }
    return {
        "comparison_id": "S13",
        "snapshot_id": "S13",
        "table": "lakehouse.audit_demo",
        "base_snapshot": {"snapshot_id": "S13-base", "files": base_files},
        "current_snapshot": {"snapshot_id": "S13-current", "files": current_files},
        "evidence_registry": evidence_registry,
        "paths": {"p7": p7, "p8": p8, "p9": p9},
    }


class SnapshotAuditStore:
    """内存态快照审计仓：每个比较一份待核清单与唯一签核结论。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._scenarios: dict[str, dict[str, Any]] = {}
        self._comparisons: dict[str, dict[str, Any]] = {}
        self._checklists: dict[str, list[dict[str, Any]]] = {}
        self._audit_todos: dict[str, list[dict[str, Any]]] = {}
        # 保留场景初始版本，reset 时可还原条目排列与证据仓
        self._scenario_templates: dict[str, dict[str, Any]] = {"S13": _build_s13()}
        self._reset("S13", self._scenario_templates["S13"])

    # --------------------------------------------------------------
    # 内部工具
    # --------------------------------------------------------------
    def _reset(self, comparison_id: str, scenario: dict[str, Any]) -> None:
        self._scenarios[comparison_id] = deepcopy(scenario)
        self._comparisons[comparison_id] = {
            "comparison_id": comparison_id,
            "snapshot_id": scenario["snapshot_id"],
            "boundary": None,                 # item / snapshot，发起审计时确定
            "status": COMPARISON_OPEN,
            "signoff_count": 0,               # 实际落账的签核次数（重试不增加）
            "attempt_count": 0,               # 含补证失败重试在内的尝试次数
            "conclusion": None,               # 同一比较只保留一条签核结论
            "conclusion_attempt": None,       # 首份结论所属尝试序号
            "audited_item_keys": [],          # 已核范围
            "waiting_item_keys": [],          # 未定项
        }
        self._checklists[comparison_id] = []
        self._audit_todos[comparison_id] = []

    def _scenario(self, comparison_id: str) -> dict[str, Any]:
        scenario = self._scenarios.get(comparison_id)
        if scenario is None:
            raise KeyError(f"比较 {comparison_id} 不存在")
        return scenario

    # --------------------------------------------------------------
    # 异常条目读取：读取快照清单里的异常（重复路径、证据缺失）条目
    # --------------------------------------------------------------
    def read_abnormal_entries(self, comparison_id: str) -> dict[str, Any]:
        with self._lock:
            scenario = self._scenario(comparison_id)
            abnormal: list[dict[str, Any]] = []
            for side_name in ("base_snapshot", "current_snapshot"):
                seen: dict[str, int] = {}
                for index, entry in enumerate(scenario[side_name]["files"]):
                    path = entry["path"]
                    seen[path] = seen.get(path, 0) + 1
                for index, entry in enumerate(scenario[side_name]["files"]):
                    path = entry["path"]
                    reasons = []
                    if seen[path] > 1:
                        reasons.append("duplicate_path")
                    registry = scenario["evidence_registry"].get(path)
                    covered = registry.get("covered_checksums", []) if registry else []
                    if entry.get("evidence") is None and entry["checksum"] not in covered:
                        reasons.append("missing_evidence")
                    if reasons:
                        abnormal.append({
                            "comparison_id": comparison_id,
                            "side": side_name,
                            "order": index,
                            "path": path,
                            "checksum": entry["checksum"],
                            "rows": entry.get("rows", 0),
                            "reasons": reasons,
                        })
            return {
                "comparison_id": comparison_id,
                "abnormal_count": len(abnormal),
                "items": abnormal,
            }

    # --------------------------------------------------------------
    # 生成快照差异待核清单：可证差异交审计，无证据差异留待核
    # --------------------------------------------------------------
    def _path_evidence(
        self, registry: dict[str, Any] | None, required_checksums: list[Any]
    ) -> dict[str, Any] | None:
        """证据判定要精确到校验值：路径级有记录但未覆盖全部相关校验值时，

        仍属于证据缺失（例如 p7 只覆盖了新校验值，重复的 orphan 校验值无证据）。
        """
        if not registry or not registry.get("available"):
            return None
        covered = registry.get("covered_checksums", [])
        if any(checksum not in covered for checksum in required_checksums):
            return None
        return registry

    def build_checklist(self, comparison_id: str) -> dict[str, Any]:
        with self._lock:
            scenario = self._scenario(comparison_id)
            registries = scenario["evidence_registry"]
            diff = snapshot_differ(
                scenario["base_snapshot"],
                scenario["current_snapshot"],
                # 先给一份路径级证据让引擎挂上原始证据对象，最终粒度在下方按
                # 校验值覆盖重新判定，避免部分覆盖被误当成已证实。
                evidence={
                    path: payload for path, payload in registries.items()
                    if payload.get("available")
                },
            )
            checklist: list[dict[str, Any]] = []
            for item in diff["diff_items"]:
                # 删除类看基线侧校验值，其余看当前侧校验值是否全部有证据覆盖
                required = (
                    item["base_checksums"] if item["change_type"] == "removed"
                    else item["current_checksums"]
                )
                registry = self._path_evidence(registries.get(item["path"]), required)
                proven = registry is not None
                checklist.append({
                    "key": item["key"],
                    "comparison_id": comparison_id,
                    "path": item["path"],
                    "change_type": item["change_type"],
                    "order": item["order"],
                    "duplicate_path": item["duplicate_path"],
                    "base_checksums": item["base_checksums"],
                    "current_checksums": item["current_checksums"],
                    "base_rows": item["base_rows"],
                    "current_rows": item["current_rows"],
                    "evidence": registry,
                    "evidence_status": STATUS_PROVEN if proven else STATUS_PENDING,
                    "signoff_status": None,       # 尚未签核
                    "signoff_attempt": None,
                    "frozen_reason": None,
                })
            # 保留原文件顺序
            checklist.sort(key=lambda row: row["order"])
            self._checklists[comparison_id] = checklist

            # 待证行数按最终（校验值粒度）判定重算：引擎只能做路径级证据拆分，
            # 部分覆盖的重复路径（p7）在此改判为待证，其行数不能按零计。
            pending_rows = sum(
                row["base_rows"] if row["change_type"] == "removed" else row["current_rows"]
                for row in checklist if row["evidence_status"] == STATUS_PENDING
            )

            # 可证差异写入审计待办；待证差异的签核证据后续汇总回待核清单
            todos = [
                {
                    "comparison_id": comparison_id,
                    "item_key": row["key"],
                    "path": row["path"],
                    "change_type": row["change_type"],
                    "evidence_status": row["evidence_status"],
                    "signoff_status": "waiting_for_audit",
                    "conclusion": None,
                }
                for row in checklist if row["evidence_status"] == STATUS_PROVEN
            ]
            self._audit_todos[comparison_id] = todos
            self._sync_audit_todo(comparison_id)

            proven_count = sum(1 for row in checklist if row["evidence_status"] == STATUS_PROVEN)
            return {
                "comparison_id": comparison_id,
                "boundary": self._comparisons[comparison_id]["boundary"],
                "total_diff_items": len(checklist),
                "proven_diff_items": proven_count,
                "pending_diff_items": len(checklist) - proven_count,
                "pending_rows": pending_rows,
                "duplicate_paths": diff["duplicate_paths"],
                "checklist": checklist,
                "audit_todo": todos,
                "order_preserved": diff["order_preserved"],
            }

    def get_checklist(self, comparison_id: str) -> dict[str, Any]:
        with self._lock:
            self._scenario(comparison_id)
            if not self._checklists.get(comparison_id):
                return self.build_checklist(comparison_id)
            comparison = self._comparisons[comparison_id]
            return {
                "comparison_id": comparison_id,
                "boundary": comparison["boundary"],
                "status": comparison["status"],
                "checklist": deepcopy(self._checklists[comparison_id]),
                "audit_todo": deepcopy(self._audit_todos[comparison_id]),
            }

    # --------------------------------------------------------------
    # 交换当前快照中同路径重复条目的排列顺序（顺序无关性验证）
    # --------------------------------------------------------------
    def reorder_duplicate_entries(self, comparison_id: str, path: str) -> dict[str, Any]:
        with self._lock:
            scenario = self._scenario(comparison_id)
            files = scenario["current_snapshot"]["files"]
            indices = [index for index, entry in enumerate(files) if entry["path"] == path]
            if len(indices) < 2:
                raise KeyError(f"路径 {path} 在当前快照中没有重复条目，无法交换顺序")
            first, second = indices[0], indices[1]
            files[first], files[second] = files[second], files[first]
            # 重排后清单需要重新生成，但签核结论仓保持不变（同一比较）
            self._checklists[comparison_id] = []
            return {
                "comparison_id": comparison_id,
                "path": path,
                "swapped_indices": [first, second],
                "current_order": [entry["checksum"] for entry in files if entry["path"] == path],
            }

    # --------------------------------------------------------------
    # 发起审计：记录本次采用的签核边界（逐项 / 整张）
    # --------------------------------------------------------------
    def open_audit(self, comparison_id: str, boundary: str) -> dict[str, Any]:
        if boundary not in (BOUNDARY_ITEM, BOUNDARY_SNAPSHOT):
            raise ValueError("boundary 只能是 item（逐项）或 snapshot（整张）")
        with self._lock:
            comparison = self._comparisons[self._scenario(comparison_id)["comparison_id"]]
            if comparison["boundary"] is not None and comparison["boundary"] != boundary:
                raise ValueError(
                    f"比较 {comparison_id} 已采用 {comparison['boundary']} 签核边界，不能中途切换"
                )
            comparison["boundary"] = boundary
            checklist_payload = self.build_checklist(comparison_id)
            if boundary == BOUNDARY_SNAPSHOT:
                # 整张冻结：只要存在待证条目，整份 S13 的签核全部冻结，
                # 包括 p8 这种本来已证实、可独立签核的条目。
                has_pending = any(
                    row["evidence_status"] == STATUS_PENDING
                    for row in self._checklists[comparison_id]
                )
                if has_pending:
                    comparison["status"] = COMPARISON_FROZEN
                    for row in self._checklists[comparison_id]:
                        row["signoff_status"] = STATUS_WAITING
                        row["frozen_reason"] = "整张签核冻结：存在待补证差异"
                    for todo in self._audit_todos[comparison_id]:
                        todo["signoff_status"] = "frozen_waiting_evidence"
            return {
                "comparison_id": comparison_id,
                "boundary": boundary,
                "status": comparison["status"],
                "checklist": deepcopy(self._checklists[comparison_id]),
                "audit_todo": deepcopy(self._audit_todos[comparison_id]),
                "summary": {
                    "proven": checklist_payload["proven_diff_items"],
                    "pending": checklist_payload["pending_diff_items"],
                },
            }

    # --------------------------------------------------------------
    # 签核：逐项边界下只签已证实条目；整张边界下待证未补齐则全部等待
    # --------------------------------------------------------------
    def sign_off(
        self,
        comparison_id: str,
        item_key: str | None = None,
        conclusion: str = "confirmed",
    ) -> dict[str, Any]:
        with self._lock:
            comparison = self._comparisons[self._scenario(comparison_id)["comparison_id"]]
            comparison["attempt_count"] += 1
            attempt = comparison["attempt_count"]
            boundary = comparison["boundary"]
            if boundary is None:
                raise ValueError("尚未发起审计，请先选择逐项或整张签核边界")

            checklist = self._checklists[comparison_id]
            target_rows = (
                [row for row in checklist if row["key"] == item_key]
                if item_key is not None else
                checklist
            )
            if item_key is not None and not target_rows:
                raise KeyError(f"待核项 {item_key} 不在比较 {comparison_id} 清单内")

            pending_rows = [row for row in target_rows if row["evidence_status"] == STATUS_PENDING]
            proven_rows = [row for row in target_rows if row["evidence_status"] == STATUS_PROVEN]

            # 整张边界：任一待证条目存在 => 整份快照冻结，p8 也一并等待
            if boundary == BOUNDARY_SNAPSHOT and pending_rows:
                for row in target_rows:
                    if row["signoff_status"] != STATUS_REJECTED:
                        row["signoff_status"] = STATUS_WAITING
                        row["frozen_reason"] = "整张签核冻结：等待 p7 补证"
                comparison["status"] = COMPARISON_FROZEN
                self._sync_audit_todo(comparison_id)
                return self._signoff_response(
                    comparison_id, attempt, accepted=False,
                    message="整张签核冻结：待证差异未补齐，p8 与其余条目一并等待补证",
                )

            # 逐项边界：无证据的差异不能签，更不能按零计。
            # 补证后清单重开会把待签行状态复位为 None，因此 None 与 PENDING
            # 都视为「尚未签核的待证项」，不能误拦已补齐证据的条目。
            if boundary == BOUNDARY_ITEM and item_key is not None and pending_rows:
                row = pending_rows[0]
                if row["signoff_status"] not in (STATUS_REJECTED,):
                    row["signoff_status"] = STATUS_PENDING
                    row["frozen_reason"] = "证据缺失，等待补证"
                self._sync_audit_todo(comparison_id)
                return self._signoff_response(
                    comparison_id, attempt, accepted=False,
                    message=f"条目 {row['path']} 证据缺失，不能签核也不能按零计，转入待核清单",
                )

            # 幂等：同一比较（整张）或同一条目（逐项）只保留首份签核结论，
            # 补证失败后的重试复用首份结果，签核次数不重复累计。
            already_signed = [row for row in proven_rows if row["signoff_status"] == STATUS_SIGNED]
            if already_signed:
                first = already_signed[0]
                return self._signoff_response(
                    comparison_id, attempt, accepted=False, reused=True,
                    message=(
                        f"复用首份签核结论（尝试 #{first['signoff_attempt']}）："
                        "同一比较只保留一条结论，重试不重复签核"
                    ),
                )

            for row in proven_rows:
                row["signoff_status"] = STATUS_SIGNED
                row["signoff_attempt"] = attempt
                row["frozen_reason"] = None
            comparison["signoff_count"] += 1
            if comparison["conclusion"] is None:
                comparison["conclusion"] = conclusion
                comparison["conclusion_attempt"] = attempt

            self._refresh_comparison_status(comparison_id)
            self._sync_audit_todo(comparison_id)
            return self._signoff_response(
                comparison_id, attempt, accepted=True,
                message=f"已证实条目签核完成，边界：{boundary}",
            )

    # --------------------------------------------------------------
    # 补证：为缺失证据的校验值补充证据；补证失败可重试，复用首份结论
    # --------------------------------------------------------------
    def supplement_evidence(
        self,
        comparison_id: str,
        path: str,
        checksum: str,
        evidence_detail: str,
        *,
        succeed: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            scenario = self._scenario(comparison_id)
            checklist = self._checklists[comparison_id]
            target = next((row for row in checklist if row["path"] == path), None)
            if target is None:
                raise KeyError(f"路径 {path} 不在待核清单内")
            if checksum not in target["current_checksums"] and checksum not in target["base_checksums"]:
                raise KeyError(f"校验值 {checksum} 不属于路径 {path} 的任何条目")

            comparison = self._comparisons[comparison_id]
            if not succeed:
                # 补证失败：保留首份结论，重试不产生新结论，签核次数不增加
                comparison["attempt_count"] += 1
                target["frozen_reason"] = "补证失败，保留首份签核结论等待再次补证"
                if target["signoff_status"] == STATUS_SIGNED:
                    target["signoff_status"] = STATUS_REJECTED
                self._sync_audit_todo(comparison_id)
                return {
                    "ok": False,
                    "comparison_id": comparison_id,
                    "path": path,
                    "checksum": checksum,
                    "attempt": comparison["attempt_count"],
                    "signoff_count": comparison["signoff_count"],
                    "message": "补证失败：首份签核结论保留，重试将复用该结论",
                }

            registry = scenario["evidence_registry"].setdefault(path, {
                "path": path, "type": "supplement", "available": True,
                "detail": "", "covered_checksums": [], "missing_checksums": [],
            })
            if checksum not in registry["covered_checksums"]:
                registry["covered_checksums"].append(checksum)
            if checksum in registry["missing_checksums"]:
                registry["missing_checksums"].remove(checksum)
            registry["available"] = True
            registry["detail"] = f"{registry.get('detail', '')} | 补证：{evidence_detail}".strip(" |")

            # 重新生成清单以获取最新证据状态，但保留已落账的签核结论
            previous_checklist = self._checklists[comparison_id]
            self.build_checklist(comparison_id)
            self._preserve_signoffs(comparison_id, previous_checklist)
            # 后补证据不能把整张快照提前标为已确认：状态只按实际签核范围重算
            self._refresh_comparison_status(comparison_id)
            self._sync_audit_todo(comparison_id)
            return {
                "ok": True,
                "comparison_id": comparison_id,
                "path": path,
                "checksum": checksum,
                "evidence_status": next(
                    row["evidence_status"]
                    for row in self._checklists[comparison_id] if row["path"] == path
                ),
                "status": comparison["status"],
                "message": "补证成功，差异清单已重开；已确认范围保持原签核结论",
            }

    def reopen_checklist(self, comparison_id: str) -> dict[str, Any]:
        """补全 p7 后重开差异清单：重新计算并返回签核次数等状态。"""
        with self._lock:
            previous_checklist = list(self._checklists[comparison_id])
            payload = self.build_checklist(comparison_id)
            self._preserve_signoffs(comparison_id, previous_checklist)
            self._refresh_comparison_status(comparison_id)
            self._sync_audit_todo(comparison_id)
            comparison = self._comparisons[comparison_id]
            payload["checklist"] = deepcopy(self._checklists[comparison_id])
            payload["status"] = comparison["status"]
            payload["signoff_count"] = comparison["signoff_count"]
            payload["attempt_count"] = comparison["attempt_count"]
            payload["conclusion"] = comparison["conclusion"]
            return payload

    # --------------------------------------------------------------
    # 审计待办与待核清单的证据汇总
    # --------------------------------------------------------------
    def _preserve_signoffs(
        self, comparison_id: str, previous: list[dict[str, Any]]
    ) -> None:
        """清单重建后恢复既有签核结论：已核范围、未定项与签核次数不被冲掉。

        后补证据会重开差异清单，但同一比较只保留一条签核结论，重建只能改变
        证据状态，不能把已签核条目退回未签、也不能提前确认整张快照。
        """
        prior = {
            row["key"]: (
                row["signoff_status"],
                row["signoff_attempt"],
                row.get("frozen_reason"),
            )
            for row in previous
        }
        for row in self._checklists[comparison_id]:
            state = prior.get(row["key"])
            if state is None:
                continue
            old_status, old_attempt, old_reason = state
            if old_status == STATUS_SIGNED:
                row["signoff_status"] = STATUS_SIGNED
                row["signoff_attempt"] = old_attempt
                row["frozen_reason"] = None
            elif old_status == STATUS_REJECTED:
                row["signoff_status"] = STATUS_REJECTED
                row["signoff_attempt"] = old_attempt
                row["frozen_reason"] = old_reason
            elif old_status == STATUS_WAITING:
                row["signoff_status"] = STATUS_WAITING
                row["frozen_reason"] = old_reason or "整张签核冻结：等待补证"

    def _sync_audit_todo(self, comparison_id: str) -> None:
        """签核结果回写审计待办；未定范围的签核证据汇总到待核清单数据。"""
        checklist = self._checklists[comparison_id]
        todos = self._audit_todos.setdefault(comparison_id, [])
        todo_by_key = {todo["item_key"]: todo for todo in todos}
        registry_all = self._scenarios[comparison_id]["evidence_registry"]
        for row in checklist:
            todo = todo_by_key.get(row["key"])
            if todo is not None:
                todo["signoff_status"] = row["signoff_status"] or "waiting_for_audit"
                todo["conclusion"] = self._comparisons[comparison_id]["conclusion"]
                todo["evidence_status"] = row["evidence_status"]
            # 每条待核项都汇总当前证据口径；未定范围（待证条目）由此可被
            # 审计工作台直接看到「为什么还不能签、还缺哪个校验值的证据」。
            row["evidence_summary"] = {
                "path": row["path"],
                "evidence_status": row["evidence_status"],
                "signoff_status": row["signoff_status"],
                "frozen_reason": row["frozen_reason"],
                "covered_checksums": registry_all.get(row["path"], {}).get(
                    "covered_checksums", []
                ),
                "missing_checksums": registry_all.get(row["path"], {}).get(
                    "missing_checksums", []
                ),
            }

    def _refresh_comparison_status(self, comparison_id: str) -> None:
        comparison = self._comparisons[comparison_id]
        checklist = self._checklists[comparison_id]
        if not checklist:
            comparison["status"] = COMPARISON_OPEN
            return
        signed = [row for row in checklist if row["signoff_status"] == STATUS_SIGNED]
        pending = [row for row in checklist if row["evidence_status"] == STATUS_PENDING]
        proven_unsigned = [
            row for row in checklist
            if row["evidence_status"] == STATUS_PROVEN and row["signoff_status"] != STATUS_SIGNED
        ]
        if comparison["boundary"] == BOUNDARY_SNAPSHOT and (pending or proven_unsigned):
            comparison["status"] = COMPARISON_FROZEN
        elif len(signed) == len(checklist):
            comparison["status"] = COMPARISON_CONFIRMED
        elif signed:
            comparison["status"] = COMPARISON_PARTIAL
        else:
            comparison["status"] = COMPARISON_OPEN
        comparison["audited_item_keys"] = [row["key"] for row in signed]
        comparison["waiting_item_keys"] = [
            row["key"] for row in checklist if row["signoff_status"] in (STATUS_WAITING, STATUS_PENDING, None)
            and row["signoff_status"] != STATUS_SIGNED
        ]

    def _signoff_response(
        self,
        comparison_id: str,
        attempt: int,
        *,
        accepted: bool,
        message: str,
        reused: bool = False,
    ) -> dict[str, Any]:
        comparison = self._comparisons[comparison_id]
        return {
            "ok": accepted,
            "reused": reused,
            "comparison_id": comparison_id,
            "boundary": comparison["boundary"],
            "attempt": attempt,
            "signoff_count": comparison["signoff_count"],
            "status": comparison["status"],
            "conclusion": comparison["conclusion"],
            "audited_item_keys": list(comparison["audited_item_keys"]),
            "waiting_item_keys": list(comparison["waiting_item_keys"]),
            "checklist": deepcopy(self._checklists[comparison_id]),
            "audit_todo": deepcopy(self._audit_todos[comparison_id]),
            "message": message,
        }

    # --------------------------------------------------------------
    # 查询
    # --------------------------------------------------------------
    def get_comparison(self, comparison_id: str) -> dict[str, Any]:
        with self._lock:
            comparison = deepcopy(self._comparisons[self._scenario(comparison_id)["comparison_id"]])
            comparison["checklist"] = deepcopy(self._checklists.get(comparison_id, []))
            comparison["audit_todo"] = deepcopy(self._audit_todos.get(comparison_id, []))
            return comparison

    def list_comparisons(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self.get_comparison(comparison_id)
                for comparison_id in sorted(self._scenarios)
            ]

    def reset(self, comparison_id: str = "S13") -> dict[str, Any]:
        with self._lock:
            template = self._scenario_templates.get(comparison_id)
            if template is None:
                raise KeyError(f"比较 {comparison_id} 不存在")
            # 从初始模板还原：交换过的条目顺序、补过的证据、签核结论全部复位
            self._reset(comparison_id, template)
            return {"ok": True, "comparison_id": comparison_id}


snapshot_audit_store = SnapshotAuditStore()
