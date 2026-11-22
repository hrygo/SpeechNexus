"""Canonical coordination DDL, taken from the generators instead of a copied file.

生产 DDL 由 `coordination_schema.COORDINATION_DDL`(13 张表)与 `job_schema.JOB_DDL`
(2 张表)生成。根目录的 `migrations/0001_cluster_coordination_draft.sql` 曾是这两处的
文本副本,不参与产品迁移链,已于本轮删除 —— 保留它等于维护两份真源。
"""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure.coordination_schema import COORDINATION_DDL
from speechnexus_backend.speech.infrastructure.job_schema import JOB_DDL

#: 与旧草案一致的 15 条建表语句,顺序为协调 schema 在前、job schema 在后。
COORDINATION_DDL_STATEMENTS: tuple[str, ...] = COORDINATION_DDL + JOB_DDL

#: 同一批语句的整段文本,供按正则检查时使用。
COORDINATION_DDL_TEXT: str = "\n".join(statement + ";" for statement in COORDINATION_DDL_STATEMENTS)

__all__ = ["COORDINATION_DDL_STATEMENTS", "COORDINATION_DDL_TEXT"]
