# SPDX-License-Identifier: GPL-3.0-only
from odc.jobs.types import (
    BACKGROUND_PRIORITY,
    DEDUPLICATED_TYPES,
    DEFAULT_PRIORITY,
    PIPELINE_PRIORITY,
    USER_ACTION_PRIORITY,
    JobType,
    PauseReason,
)
from odc.storage.tables import JOB_TYPES, PAUSE_REASONS
from odc.telemetry.metrics import JobType as MetricsJobType


def test_vocabulario_acompanha_o_schema_do_banco() -> None:
    assert tuple(t.value for t in JobType) == JOB_TYPES
    assert tuple(r.value for r in PauseReason) == PAUSE_REASONS


def test_vocabulario_acompanha_o_catalogo_de_metricas() -> None:
    assert {t.value for t in JobType} == {t.value for t in MetricsJobType}


def test_prioridades_do_plan() -> None:
    assert {DEFAULT_PRIORITY[t] for t in (JobType.MOVE, JobType.RESTORE, JobType.DELETE)} == {
        USER_ACTION_PRIORITY
    }
    assert {
        DEFAULT_PRIORITY[t] for t in (JobType.TRAIN, JobType.CLASSIFY, JobType.AUTO_ACTIONS)
    } == {PIPELINE_PRIORITY}
    assert {DEFAULT_PRIORITY[t] for t in (JobType.SCAN, JobType.EXTRACT, JobType.CLEANUP)} == {
        BACKGROUND_PRIORITY
    }
    assert (USER_ACTION_PRIORITY, PIPELINE_PRIORITY, BACKGROUND_PRIORITY) == (100, 50, 10)
    assert set(DEFAULT_PRIORITY) == set(JobType)


def test_tipos_deduplicados() -> None:
    assert {JobType.SCAN, JobType.TRAIN} == DEDUPLICATED_TYPES
