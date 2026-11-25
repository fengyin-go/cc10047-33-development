"""快照差异审计路由：异常条目读取、待核清单、发起审计、签核与补证。"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.snapshot_audit import (
    BOUNDARY_ITEM,
    BOUNDARY_SNAPSHOT,
    snapshot_audit_store,
)

router = APIRouter(prefix="/api/snapshot-audit", tags=["快照差异审计"])


class OpenAuditPayload(BaseModel):
    boundary: Literal["item", "snapshot"] = Field(
        description="item=逐项签核（p8 可先签、p7 等待补证）；snapshot=整张冻结"
    )


class SignOffPayload(BaseModel):
    item_key: str | None = Field(default=None, description="为空时按整张范围签核")
    conclusion: str = "confirmed"


class EvidencePayload(BaseModel):
    path: str
    checksum: str
    evidence_detail: str
    succeed: bool = True


def _handle_key_error(exc: KeyError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc).strip("'"))


@router.get("/comparisons")
def list_comparisons() -> dict[str, Any]:
    return {"items": snapshot_audit_store.list_comparisons()}


@router.get("/comparisons/{comparison_id}")
def get_comparison(comparison_id: str) -> dict[str, Any]:
    try:
        return snapshot_audit_store.get_comparison(comparison_id)
    except KeyError as exc:
        raise _handle_key_error(exc)


@router.get("/comparisons/{comparison_id}/abnormal-entries")
def read_abnormal_entries(comparison_id: str) -> dict[str, Any]:
    """新增异常条目读取：重复路径、证据缺失条目。"""
    try:
        return snapshot_audit_store.read_abnormal_entries(comparison_id)
    except KeyError as exc:
        raise _handle_key_error(exc)


@router.post("/comparisons/{comparison_id}/checklist")
def build_checklist(comparison_id: str) -> dict[str, Any]:
    """生成快照差异待核清单数据：可证差异交审计，无证据差异留待核。"""
    try:
        return snapshot_audit_store.build_checklist(comparison_id)
    except KeyError as exc:
        raise _handle_key_error(exc)


@router.get("/comparisons/{comparison_id}/checklist")
def get_checklist(comparison_id: str) -> dict[str, Any]:
    try:
        return snapshot_audit_store.get_checklist(comparison_id)
    except KeyError as exc:
        raise _handle_key_error(exc)


@router.post("/comparisons/{comparison_id}/reorder")
def reorder_entries(comparison_id: str, path: str) -> dict[str, Any]:
    """交换同路径重复条目的排列顺序，验证顺序无关的差异判定。"""
    try:
        return snapshot_audit_store.reorder_duplicate_entries(comparison_id, path)
    except KeyError as exc:
        raise _handle_key_error(exc)


@router.post("/comparisons/{comparison_id}/audit")
def open_audit(comparison_id: str, payload: OpenAuditPayload) -> dict[str, Any]:
    """发起审计并记录本次采用逐项（item）还是整张（snapshot）签核边界。"""
    try:
        return snapshot_audit_store.open_audit(comparison_id, payload.boundary)
    except KeyError as exc:
        raise _handle_key_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/comparisons/{comparison_id}/signoff")
def sign_off(comparison_id: str, payload: SignOffPayload) -> dict[str, Any]:
    try:
        return snapshot_audit_store.sign_off(
            comparison_id, item_key=payload.item_key, conclusion=payload.conclusion
        )
    except KeyError as exc:
        raise _handle_key_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/comparisons/{comparison_id}/evidence")
def supplement_evidence(comparison_id: str, payload: EvidencePayload) -> dict[str, Any]:
    """补证（可模拟补证失败）：失败时保留首份结论，重试复用。"""
    try:
        return snapshot_audit_store.supplement_evidence(
            comparison_id,
            path=payload.path,
            checksum=payload.checksum,
            evidence_detail=payload.evidence_detail,
            succeed=payload.succeed,
        )
    except KeyError as exc:
        raise _handle_key_error(exc)


@router.post("/comparisons/{comparison_id}/reopen")
def reopen_checklist(comparison_id: str) -> dict[str, Any]:
    """补全 p7 后重开差异清单，并回传签核次数与已核范围。"""
    try:
        return snapshot_audit_store.reopen_checklist(comparison_id)
    except KeyError as exc:
        raise _handle_key_error(exc)


@router.post("/comparisons/{comparison_id}/reset")
def reset_comparison(comparison_id: str) -> dict[str, Any]:
    try:
        return snapshot_audit_store.reset(comparison_id)
    except KeyError as exc:
        raise _handle_key_error(exc)


@router.get("/boundaries")
def boundaries() -> dict[str, str]:
    return {"item": BOUNDARY_ITEM, "snapshot": BOUNDARY_SNAPSHOT}
