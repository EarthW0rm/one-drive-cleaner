# SPDX-License-Identifier: GPL-3.0-only
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert

from odc.metrics_summary.service import (
    DISK_BUDGET_BYTES,
    Alert,
    MetricsSummary,
    MetricsSummaryService,
)
from odc.ml.evaluator import ModelMetrics
from odc.storage.engine import create_engine_for
from odc.storage.migrate import upgrade
from odc.storage.repositories import app_meta, categories, jobs, journal, labels, models
from odc.storage.repositories import quarantine as quarantine_repo
from odc.storage.repositories import settings as settings_repo
from odc.storage.tables import embedding_model
from odc.storage.uow import UnitOfWork

from .conftest import NOW, STAMP, Env, ItemSeeder

MIB = 1024**2


def _codes(summary: MetricsSummary) -> list[str]:
    return [alert.code for alert in summary.alerts]


def _iso(delta: timedelta) -> str:
    return (NOW - delta).isoformat()


def _move(  # noqa: PLR0913 - cenário de teste parametrizável
    env: Env,
    item_ids: list[int],
    *,
    moved_at: str = STAMP,
    restored_at: str | None = None,
    reason: str = "false_positive",
    restored: int = 0,
) -> None:
    """Cria entradas ativas e restaura as `restored` primeiras com o motivo informado."""
    with UnitOfWork(env.holder.engine) as session:
        for position, item_id in enumerate(item_ids):
            entry = quarantine_repo.create(
                session,
                item_id=item_id,
                original_parent_id="p",
                original_parent_path="/a",
                original_name="x",
                quarantined_name="x",
                mode="auto",
                now=moved_at,
            )
            if position < restored:
                quarantine_repo.mark_restored(
                    session,
                    entry.id,
                    reason=reason,
                    fallback=False,
                    now=restored_at or moved_at,
                )


def _set(env: Env, **values: object) -> None:
    with UnitOfWork(env.holder.engine) as session:
        for key, value in values.items():
            settings_repo.set_value(session, key, value, now=STAMP)


def _metrics_json(precision: float | None) -> str:
    return ModelMetrics(
        per_category=(),
        removable_precision=precision,
        removable_support=40,
        macro_f1=0.9,
        confusion=(),
        labels=(),
        holdout_size=0,
        threshold=0.98,
        holdout_digest="d",
    ).to_json()


def _model(env: Env, metrics: str | None, status: str = "candidate") -> int:
    with UnitOfWork(env.holder.engine) as session:
        session.execute(
            insert(embedding_model).prefix_with("OR IGNORE"),
            [{"id": 1, "name": "f", "dim": 4, "thumb_size": "c", "is_active": 1}],
        )
        version = models.create_candidate(
            session,
            strategy="logreg",
            embedding_model_id=1,
            labels_count=1,
            categories=["screenshot"],
            params=b"",
            metrics=metrics,
            trigger="manual",
            now=STAMP,
        )
        if status == "current":
            models.promote(session, version.id)
    return version.id


def test_banco_vazio_nao_tem_alertas_e_taxas_nulas(env: Env) -> None:
    summary = env.service.summary()

    assert summary.by_state == {}
    assert summary.backlog == {"extract": 0, "classify": 0, "review": 0}
    assert (summary.quarantine_items, summary.quarantine_bytes) == (0, 0)
    assert summary.quarantine_by_category == {}
    assert (summary.false_positive_last_7d, summary.false_positive_total) == (None, None)
    assert summary.disk == {
        "temp": 0,
        "db": summary.disk["db"],
        "model": 0,
        "logs": 0,
        "budget_bytes": 524288000,
    }
    assert summary.models == ()
    assert summary.alerts == ()
    assert summary.labels_total == summary.labels_since_last_training == 0


def test_itens_por_estado_e_backlog_por_etapa(env: Env, seed_items: ItemSeeder) -> None:
    seed_items({"DESCOBERTO": 3, "ANALISADO": 2, "SUGERIDO": 4, "EXCLUIDO": 1})

    summary = env.service.summary()

    assert summary.by_state == {"DESCOBERTO": 3, "ANALISADO": 2, "SUGERIDO": 4, "EXCLUIDO": 1}
    assert summary.backlog == {"extract": 3, "classify": 2, "review": 4}


def test_quarentena_por_categoria_inclui_sem_categoria(env: Env, seed_items: ItemSeeder) -> None:
    ids = seed_items({"QUARENTENA": 3})
    with UnitOfWork(env.holder.engine) as session:
        screenshot = categories.get_by_slug(session, "screenshot")
        assert screenshot is not None
        for item_id in ids[:2]:
            labels.add_many(
                session, [labels.LabelInput(item_id, screenshot.id, "review")], now=STAMP
            )
    _move(env, ids)

    summary = env.service.summary()

    assert summary.quarantine_items == 3
    assert summary.quarantine_bytes == 30
    assert summary.quarantine_by_category == {str(screenshot.id): 2, "unclassified": 1}
    assert summary.labels_total == 2


def test_taxa_de_falso_positivo_50_movidos_1_restaurado(env: Env, seed_items: ItemSeeder) -> None:
    _set(env, automation_mode="auto")
    _move(env, seed_items({"SUGERIDO": 50}), restored=1)

    summary = env.service.summary()

    assert summary.false_positive_last_7d == pytest.approx(0.02)
    assert summary.false_positive_total == pytest.approx(0.02)
    assert "HIGH_FALSE_POSITIVE" not in _codes(summary)


def test_restauracao_por_outro_motivo_nao_conta_como_falso_positivo(
    env: Env, seed_items: ItemSeeder
) -> None:
    _move(env, seed_items({"SUGERIDO": 10}), restored=3, reason="manual")

    assert env.service.summary().false_positive_total == 0.0


def test_janela_de_7_dias_separa_recentes_de_antigos(env: Env, seed_items: ItemSeeder) -> None:
    ids = seed_items({"SUGERIDO": 14})
    _move(env, ids[:10], moved_at=_iso(timedelta(days=30)), restored=5)
    _move(
        env,
        ids[10:],
        moved_at=_iso(timedelta(days=1)),
        restored=1,
        restored_at=_iso(timedelta(days=1)),
    )

    summary = env.service.summary()

    assert summary.false_positive_last_7d == pytest.approx(0.25)
    assert summary.false_positive_total == pytest.approx(6 / 14)


def test_alerta_falso_positivo_alto_so_com_automatico_efetivo(
    env: Env, seed_items: ItemSeeder
) -> None:
    _move(env, seed_items({"SUGERIDO": 10}), moved_at=_iso(timedelta(days=1)), restored=2)

    _set(env, automation_mode="manual")
    assert "HIGH_FALSE_POSITIVE" not in _codes(env.service.summary())

    _set(env, automation_mode="auto")
    env.tick[0] += 10
    summary = env.service.summary()

    assert _codes(summary) == ["HIGH_FALSE_POSITIVE"]
    assert summary.alerts[0].since == NOW.isoformat()


def test_alerta_diario_pendente_apos_5_minutos(env: Env, seed_items: ItemSeeder) -> None:
    item_id = seed_items({"SUGERIDO": 1})[0]
    with UnitOfWork(env.holder.engine) as session:
        journal.begin(
            session,
            op_id="recente",
            operation="move",
            now=_iso(timedelta(minutes=2)),
            item_id=item_id,
        )
    assert env.service.summary().alerts == ()

    old = _iso(timedelta(minutes=6))
    with UnitOfWork(env.holder.engine) as session:
        journal.begin(session, op_id="antiga", operation="move", now=old, item_id=item_id)
    env.tick[0] += 10

    assert env.service.summary().alerts == (Alert("JOURNAL_PENDING", old),)


def test_diario_com_instante_invalido_nao_dispara(env: Env) -> None:
    with UnitOfWork(env.holder.engine) as session:
        journal.begin(session, op_id="x", operation="ensure_folder", now="lixo")

    assert env.service.summary().alerts == ()


def test_alerta_temporario_no_limite_do_teto(env: Env) -> None:
    _set(env, temp_cap_bytes=1000)
    (env.dirs.temp / "a.bin").write_bytes(b"x" * 899)
    assert env.service.summary().alerts == ()

    (env.dirs.temp / "b.bin").write_bytes(b"x" * 1)
    env.tick[0] += 10
    summary = env.service.summary()

    assert _codes(summary) == ["TEMP_NEAR_CAP"]
    assert summary.disk["temp"] == 900


def test_teto_zero_desativa_alerta_de_temporario(env: Env) -> None:
    _set(env, temp_cap_bytes=0)
    (env.dirs.temp / "a.bin").write_bytes(b"x" * 10)

    assert env.service.summary().alerts == ()


def test_alerta_login_exigido_por_job_pausado(env: Env) -> None:
    with UnitOfWork(env.holder.engine) as session:
        job = jobs.enqueue(session, job_type="scan", payload={}, now=STAMP)
        jobs.pause(session, job.id, reason="login_required")
        other = jobs.enqueue(session, job_type="extract", payload={}, now=_iso(timedelta(days=1)))
        jobs.pause(session, other.id, reason="user")

    assert env.service.summary().alerts == (Alert("LOGIN_REQUIRED", STAMP),)


def test_alerta_login_exigido_pelo_gateway_de_autenticacao(env: Env) -> None:
    env.login[0] = True

    assert env.service.summary().alerts == (Alert("LOGIN_REQUIRED", NOW.isoformat()),)


def test_alerta_disco_acima_da_meta_ignora_pesos(env: Env) -> None:
    with (env.dirs.models / "w.bin").open("wb") as handle:
        handle.truncate(DISK_BUDGET_BYTES + MIB)
    assert env.service.summary().alerts == ()

    with (env.dirs.logs / "l.log").open("wb") as handle:
        handle.truncate(DISK_BUDGET_BYTES + MIB)
    env.tick[0] += 10

    summary = env.service.summary()
    assert _codes(summary) == ["DISK_OVER_BUDGET"]
    assert summary.disk["model"] > DISK_BUDGET_BYTES


def test_alerta_rodada_automatica_limitada_nas_ultimas_24h(env: Env) -> None:
    recent = _iso(timedelta(hours=23))
    with UnitOfWork(env.holder.engine) as session:
        app_meta.set_value(session, "auto_move_capped_at", recent)
        app_meta.set_value(session, "auto_move_capped_count", "12")

    assert env.service.summary().alerts == (Alert("AUTO_MOVE_CAPPED", recent),)

    with UnitOfWork(env.holder.engine) as session:
        app_meta.set_value(session, "auto_move_capped_at", _iso(timedelta(hours=25)))
    env.tick[0] += 10
    assert env.service.summary().alerts == ()

    with UnitOfWork(env.holder.engine) as session:
        app_meta.set_value(session, "auto_move_capped_at", "lixo")
    env.tick[0] += 10
    assert env.service.summary().alerts == ()


def test_alerta_sem_instante_persistido_mantem_o_primeiro_visto(env: Env) -> None:
    env.login[0] = True
    first = env.service.summary()
    env.tick[0] += 10
    later = MetricsSummaryService(
        env.holder, clock=lambda: NOW + timedelta(hours=1), login_required=lambda: True
    )
    assert later.summary().alerts[0].since == (NOW + timedelta(hours=1)).isoformat()

    env.login[0] = False
    assert env.service.summary().alerts == ()
    env.login[0] = True
    env.tick[0] += 10
    assert env.service.summary().alerts == first.alerts


def test_modelos_recentes_em_ordem_cronologica_com_precisao(env: Env) -> None:
    first = _model(env, _metrics_json(0.9))
    second = _model(env, "{invalido")
    third = _model(env, _metrics_json(0.99), status="current")

    points = env.service.summary().models

    assert [(p.id, p.status, p.removable_precision) for p in points] == [
        (first, "candidate", 0.9),
        (second, "candidate", None),
        (third, "current", 0.99),
    ]


def test_apenas_as_10_ultimas_versoes(env: Env) -> None:
    ids = [_model(env, None) for _ in range(12)]

    assert [p.id for p in env.service.summary().models] == ids[2:]


def test_rotulos_desde_o_ultimo_treino(env: Env, seed_items: ItemSeeder) -> None:
    ids = seed_items({"SUGERIDO": 3})
    with UnitOfWork(env.holder.engine) as session:
        screenshot = categories.get_by_slug(session, "screenshot")
        assert screenshot is not None
        labels.add_many(session, [labels.LabelInput(ids[0], screenshot.id, "review")], now=STAMP)
    _model(env, None)
    with UnitOfWork(env.holder.engine) as session:
        labels.add_many(
            session,
            [labels.LabelInput(i, screenshot.id, "review") for i in ids[1:]],
            now=NOW.isoformat(),
        )

    summary = env.service.summary()

    assert (summary.labels_total, summary.labels_since_last_training) == (3, 2)


def test_cache_de_5_segundos_e_invalidado_ao_trocar_o_banco(
    env: Env, seed_items: ItemSeeder, tmp_path: Path
) -> None:
    first = env.service.summary()
    seed_items({"DESCOBERTO": 1})

    assert env.service.summary() is first
    env.tick[0] += 5.1
    assert env.service.summary().by_state == {"DESCOBERTO": 1}

    upgrade(tmp_path / "outra.db")
    original = env.holder.engine
    env.holder.engine = create_engine_for(tmp_path / "outra.db")
    try:
        assert env.service.summary().by_state == {}
    finally:
        env.holder.engine.dispose()
        env.holder.engine = original


def test_desempenho_com_50_mil_itens(env: Env, seed_items: ItemSeeder) -> None:
    ids = seed_items(
        {"DESCOBERTO": 10_000, "ANALISADO": 10_000, "SUGERIDO": 25_000, "QUARENTENA": 5_000}
    )
    _move(env, ids[-5_000:], moved_at=_iso(timedelta(days=1)), restored=0)
    for number in range(3):
        _model(env, _metrics_json(0.95 + number / 100))

    start = time.perf_counter()
    summary = env.service.summary()
    elapsed = time.perf_counter() - start

    assert sum(summary.by_state.values()) == 50_000
    assert summary.quarantine_items == 5_000
    assert elapsed <= 0.3
