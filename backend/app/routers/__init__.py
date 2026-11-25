"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, chains, snapshot_audit

# snapshot_audit 含 /api/snapshot-audit 固定前缀，且不与资源模块 key 冲突，
# 保持在资源动态路由之前注册，避免固定路径被 /api/{module} 抢先匹配。
ROUTERS = [snapshot_audit, resource, chains]
