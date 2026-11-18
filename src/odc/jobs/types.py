# SPDX-License-Identifier: GPL-3.0-only
"""Vocabulário da fila: tipos de job, motivos de pausa e prioridades (plan 3.1, 4.3)."""

from collections.abc import Mapping
from enum import StrEnum


class JobType(StrEnum):
    SCAN = "scan"
    EXTRACT = "extract"
    CLASSIFY = "classify"
    TRAIN = "train"
    AUTO_ACTIONS = "auto_actions"
    MOVE = "move"
    RESTORE = "restore"
    DELETE = "delete"
    RECONCILE = "reconcile"
    CLEANUP = "cleanup"


class PauseReason(StrEnum):
    LOGIN_REQUIRED = "login_required"
    TEMP_CAP = "temp_cap"
    USER = "user"
    NETWORK = "network"


USER_ACTION_PRIORITY = 100
PIPELINE_PRIORITY = 50
BACKGROUND_PRIORITY = 10

# Ações do utilizador passam à frente do pipeline, que passa à frente do trabalho de fundo.
DEFAULT_PRIORITY: Mapping[JobType, int] = {
    JobType.MOVE: USER_ACTION_PRIORITY,
    JobType.RESTORE: USER_ACTION_PRIORITY,
    JobType.DELETE: USER_ACTION_PRIORITY,
    JobType.TRAIN: PIPELINE_PRIORITY,
    JobType.CLASSIFY: PIPELINE_PRIORITY,
    JobType.AUTO_ACTIONS: PIPELINE_PRIORITY,
    JobType.SCAN: BACKGROUND_PRIORITY,
    JobType.EXTRACT: BACKGROUND_PRIORITY,
    JobType.CLEANUP: BACKGROUND_PRIORITY,
    # `reconcile` não está na tabela do plan 4.3; roda no startup e é curto: fica no fundo.
    JobType.RECONCILE: BACKGROUND_PRIORITY,
}

# Tipos com no máximo um job ativo por vez (use `JobQueue.enqueue_unique`).
DEDUPLICATED_TYPES: frozenset[JobType] = frozenset({JobType.SCAN, JobType.TRAIN})
