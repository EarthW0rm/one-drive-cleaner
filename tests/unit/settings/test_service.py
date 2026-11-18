# SPDX-License-Identifier: GPL-3.0-only
import pytest
from pydantic import ValidationError

from odc.jobs.payloads import AutoActionsPayload
from odc.jobs.queue import JobQueue
from odc.jobs.types import JobType
from odc.settings.service import MIN_TEMP_CAP_BYTES, SettingsPatch, SettingsService
from odc.storage.account_db import AccountDatabase


def _service(database: AccountDatabase) -> SettingsService:
    return SettingsService(database)


def _patch(**fields: object) -> SettingsPatch:
    return SettingsPatch.model_validate(fields)


def _queued_auto_actions(database: AccountDatabase) -> list[AutoActionsPayload]:
    queue = JobQueue(database)
    return [
        p for p in queue.active_payloads(JobType.AUTO_ACTIONS) if isinstance(p, AutoActionsPayload)
    ]


def test_get_devolve_padroes_sem_gate(database: AccountDatabase) -> None:
    view = _service(database).get()

    assert view.automation_mode == "evolutive"
    assert view.auto_threshold == 0.98
    assert view.retrain_every == 20
    assert view.temp_cap_bytes == 2 * 1024**3
    assert view.otel_enabled is True
    assert view.otel_endpoint == "http://127.0.0.1:4317"
    assert view.restart_required is False
    assert not hasattr(view, "gate_precision")


def test_update_grava_e_devolve_valores_novos(database: AccountDatabase) -> None:
    result = _service(database).update(_patch(retrain_every=50, temp_cap_bytes=MIN_TEMP_CAP_BYTES))

    assert result.view.retrain_every == 50
    assert result.view.temp_cap_bytes == MIN_TEMP_CAP_BYTES
    assert _service(database).get().retrain_every == 50


def test_update_sem_campos_nao_altera_nada(database: AccountDatabase) -> None:
    result = _service(database).update(_patch())

    assert result.view.automation_mode == "evolutive"
    assert result.auto_actions_job_id is None


@pytest.mark.parametrize(
    "fields",
    [
        {"auto_threshold": 1.0},
        {"auto_threshold": 0.4},
        {"retrain_every": 4},
        {"retrain_every": 501},
        {"temp_cap_bytes": MIN_TEMP_CAP_BYTES - 1},
        {"automation_mode": "outro"},
        {"otel_endpoint": "http://exemplo.com:4317"},
        {"otel_endpoint": "ftp://127.0.0.1:4317"},
        {"gate_precision": 0.5},
        {"gate_min_labels": 1},
        {"gate_min_support": 1},
        {"automation_mode": None},
    ],
)
def test_patch_invalido_levanta_validation_error(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SettingsPatch.model_validate(fields)


def test_patch_aceita_limites_inclusivos() -> None:
    patch = _patch(auto_threshold=0.5, retrain_every=5, otel_endpoint="http://localhost:4317")

    assert patch.auto_threshold == 0.5


def test_alterar_otel_endpoint_exige_reinicio(database: AccountDatabase) -> None:
    result = _service(database).update(_patch(otel_endpoint="http://localhost:4318"))

    assert result.view.restart_required is True
    assert result.view.otel_endpoint == "http://localhost:4318"


def test_alterar_otel_enabled_exige_reinicio(database: AccountDatabase) -> None:
    assert _service(database).update(_patch(otel_enabled=False)).view.restart_required is True


def test_regravar_mesmo_otel_nao_exige_reinicio(database: AccountDatabase) -> None:
    result = _service(database).update(_patch(otel_endpoint="http://127.0.0.1:4317"))

    assert result.view.restart_required is False


def test_alterar_modo_sem_modelo_vigente_nao_enfileira(database: AccountDatabase) -> None:
    result = _service(database).update(_patch(automation_mode="auto"))

    assert result.auto_actions_job_id is None
    assert _queued_auto_actions(database) == []


def test_alterar_modo_enfileira_auto_actions_com_modelo_vigente(
    database: AccountDatabase, with_current_model: None
) -> None:
    result = _service(database).update(_patch(automation_mode="auto"))

    assert result.auto_actions_job_id is not None
    assert _queued_auto_actions(database) == [AutoActionsPayload(model_version_id=7)]


def test_alterar_limiar_enfileira_auto_actions(
    database: AccountDatabase, with_current_model: None
) -> None:
    result = _service(database).update(_patch(auto_threshold=0.99))

    assert result.auto_actions_job_id is not None


def test_regravar_mesmo_modo_nao_enfileira(
    database: AccountDatabase, with_current_model: None
) -> None:
    result = _service(database).update(_patch(automation_mode="evolutive"))

    assert result.auto_actions_job_id is None


def test_alterar_retreino_nao_enfileira(
    database: AccountDatabase, with_current_model: None
) -> None:
    result = _service(database).update(_patch(retrain_every=30))

    assert result.auto_actions_job_id is None
