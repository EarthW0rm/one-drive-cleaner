# SPDX-License-Identifier: GPL-3.0-only
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest
from tests.fakes.embedder_fake import FakeEmbedder
from tests.unit.ml.handler_support import category_id, make_db
from tests.unit.scanning.support import Database

from odc.ml.categories import CategoryService, prompts_from, slugify
from odc.ml.zero_shot import ZeroShotClassifier
from odc.storage.errors import (
    ConflictError,
    NotFoundError,
    ProtectedCategoryError,
    StorageValidationError,
)
from odc.storage.repositories import categories as categories_repo
from odc.storage.repositories import items, labels, models, scan_roots
from odc.storage.uow import UnitOfWork

NOW = "2026-11-17T10:00:00+00:00"


@dataclass
class _Enqueued:
    job_id: int = 1
    created: bool = True


@dataclass
class _Queue:
    calls: list[str] = field(default_factory=list)

    def enqueue_classify_all(self) -> int:
        self.calls.append("classify")
        return 1

    def enqueue_train(self, trigger: Literal["threshold", "manual"]) -> _Enqueued:
        self.calls.append(f"train:{trigger}")
        return _Enqueued()

    def enqueue_auto_actions(self, model_version_id: int) -> int:
        self.calls.append(f"auto_actions:{model_version_id}")
        return 1


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return make_db(tmp_path)


@pytest.fixture
def queue() -> _Queue:
    return _Queue()


@pytest.fixture
def service(db: Database, queue: _Queue) -> CategoryService:
    return CategoryService(db, queue)


def _promote_model(db: Database) -> int:
    with UnitOfWork(db.engine) as session:
        version = models.create_candidate(
            session,
            strategy="logreg",
            embedding_model_id=1,
            labels_count=1,
            categories=["screenshot"],
            params=b"",
            metrics=None,
            trigger="manual",
            now=NOW,
        )
        models.promote(session, version.id)
        return version.id


def _add_label(db: Database, cid: int, item_key: str) -> None:
    with UnitOfWork(db.engine) as session:
        roots = scan_roots.list_roots(session)
        root = (
            roots[0].id
            if roots
            else scan_roots.add(session, drive_item_id="r", display_path="/p").id
        )
        item = items.upsert_from_delta(
            session,
            items.DeltaItem(
                drive_item_id=item_key,
                scan_root_id=root,
                parent_id="p",
                name="n",
                mime_type="image/jpeg",
                size_bytes=1,
                remote_created_at=NOW,
                remote_modified_at=NOW,
                ctag="c",
                etag="e",
            ),
            now=NOW,
        )
        labels.add_many(session, [labels.LabelInput(item.id, cid, "review")], now=NOW)


def test_slugify_normaliza_acentos_e_separadores() -> None:
    assert slugify("  Cupom Fiscal — Ótimo! ") == "cupom_fiscal_otimo"


def test_slugify_sem_alfanumerico_e_invalido() -> None:
    with pytest.raises(StorageValidationError):
        slugify("!!! ---")


def test_prompts_usam_descricao_direta() -> None:
    assert prompts_from("um cupom") == ("um cupom", "a photo of um cupom")


def test_create_persiste_com_prompts_e_enfileira_classify(
    service: CategoryService, queue: _Queue
) -> None:
    change = service.create(name=" Cupom ", description="cupom fiscal", removable=True)

    row = change.view.row
    assert (row.slug, row.name, row.removable, row.protected) == ("cupom", "Cupom", True, False)
    assert row.zero_shot_prompts == prompts_from("cupom fiscal")
    assert change.view.labels == 0
    assert change.queued
    assert queue.calls == ["classify"]


def test_create_nome_duplicado_sem_distinguir_caixa(service: CategoryService) -> None:
    service.create(name="Cupom", description="d", removable=True)

    with pytest.raises(ConflictError):
        service.create(name="cupom", description="outra", removable=False)


def test_create_nome_igual_ao_de_categoria_semeada(service: CategoryService) -> None:
    with pytest.raises(ConflictError):
        service.create(name="FOTO REAL", description="d", removable=False)


def test_create_slug_colidente_com_nome_diferente(service: CategoryService) -> None:
    service.create(name="Cupom!", description="d", removable=True)

    with pytest.raises(ConflictError):
        service.create(name="Cupom?", description="d", removable=True)


@pytest.mark.parametrize(
    ("name", "description"), [("", "d"), ("x" * 61, "d"), ("ok", ""), ("ok", "d" * 301)]
)
def test_create_valida_tamanhos(service: CategoryService, name: str, description: str) -> None:
    with pytest.raises(StorageValidationError):
        service.create(name=name, description=description, removable=True)


def test_categoria_nova_entra_no_zero_shot(db: Database, service: CategoryService) -> None:
    created = service.create(name="Cupom", description="cupom fiscal", removable=True).view.row
    with UnitOfWork(db.engine, write=False) as session:
        active = categories_repo.list_active(session)
    classifier = ZeroShotClassifier(FakeEmbedder(dim=16))

    classifier.prepare(active)

    assert created.id in classifier.category_ids


def test_foto_real_nao_pode_ser_alterada(
    db: Database, service: CategoryService, queue: _Queue
) -> None:
    real = category_id(db, "real_photo")

    for fields in (
        {"name": "Outra"},
        {"description": "x"},
        {"removable": True},
        {"archived": True},
    ):
        with pytest.raises(ProtectedCategoryError):
            service.update(real, **fields)
    assert queue.calls == []


def test_update_inexistente(service: CategoryService) -> None:
    with pytest.raises(NotFoundError):
        service.update(9999, name="x")


def test_renomear_nao_enfileira_e_mantem_slug(service: CategoryService, queue: _Queue) -> None:
    created = service.create(name="Cupom", description="d", removable=True).view.row
    queue.calls.clear()

    change = service.update(created.id, name="Recibo")

    assert (change.view.row.name, change.view.row.slug) == ("Recibo", "cupom")
    assert not change.queued
    assert queue.calls == []


def test_renomear_para_nome_existente_conflita_e_mesmo_nome_passa(
    service: CategoryService,
) -> None:
    a = service.create(name="Cupom", description="d", removable=True).view.row
    service.create(name="Recibo", description="d", removable=True)

    with pytest.raises(ConflictError):
        service.update(a.id, name="recibo")
    assert service.update(a.id, name="Cupom").view.row.name == "Cupom"


def test_alterar_descricao_regera_prompts_e_reclassifica(
    service: CategoryService, queue: _Queue
) -> None:
    created = service.create(name="Cupom", description="d", removable=True).view.row
    queue.calls.clear()

    change = service.update(created.id, description="nova")

    assert change.view.row.zero_shot_prompts == prompts_from("nova")
    assert queue.calls == ["classify"]


def test_descricao_igual_nao_reclassifica(service: CategoryService, queue: _Queue) -> None:
    created = service.create(name="Cupom", description="d", removable=True).view.row
    queue.calls.clear()

    service.update(created.id, description="d")

    assert queue.calls == []


def test_tornar_nao_removivel_enfileira_auto_actions_do_modelo_vigente(
    db: Database, service: CategoryService, queue: _Queue
) -> None:
    version = _promote_model(db)
    created = service.create(name="Cupom", description="d", removable=True).view.row
    queue.calls.clear()

    change = service.update(created.id, removable=False)

    assert not change.view.row.removable
    assert queue.calls == [f"auto_actions:{version}"]


def test_tornar_nao_removivel_sem_modelo_nao_enfileira(
    service: CategoryService, queue: _Queue
) -> None:
    created = service.create(name="Cupom", description="d", removable=True).view.row
    queue.calls.clear()

    change = service.update(created.id, removable=False)

    assert not change.queued
    assert queue.calls == []


def test_tornar_removivel_nao_enfileira(
    db: Database, service: CategoryService, queue: _Queue
) -> None:
    _promote_model(db)
    created = service.create(name="Cupom", description="d", removable=False).view.row
    queue.calls.clear()

    service.update(created.id, removable=True)

    assert queue.calls == []


def test_arquivar_e_reativar_reclassificam_e_retreinam(
    db: Database, service: CategoryService, queue: _Queue
) -> None:
    _promote_model(db)
    created = service.create(name="Cupom", description="d", removable=True).view.row
    queue.calls.clear()

    archived = service.update(created.id, archived=True)
    assert archived.view.row.archived_at is not None
    assert queue.calls == ["classify", "train:manual"]
    with UnitOfWork(db.engine, write=False) as session:
        assert created.id not in [c.id for c in categories_repo.list_active(session)]

    queue.calls.clear()
    reactivated = service.update(created.id, archived=False)
    assert reactivated.view.row.archived_at is None
    assert queue.calls == ["classify", "train:manual"]


def test_arquivar_sem_modelo_so_reclassifica(service: CategoryService, queue: _Queue) -> None:
    created = service.create(name="Cupom", description="d", removable=True).view.row
    queue.calls.clear()

    service.update(created.id, archived=True)

    assert queue.calls == ["classify"]


def test_arquivar_ja_arquivada_e_reativar_ativa_sao_noop(
    service: CategoryService, queue: _Queue
) -> None:
    created = service.create(name="Cupom", description="d", removable=True).view.row
    service.update(created.id, archived=True)
    queue.calls.clear()

    service.update(created.id, archived=True)
    service.update(created.id, archived=False)
    queue.calls.clear()
    service.update(created.id, archived=False)

    assert queue.calls == []


def test_list_all_inclui_arquivadas_e_conta_rotulos_vigentes(
    db: Database, service: CategoryService
) -> None:
    created = service.create(name="Cupom", description="d", removable=True).view.row
    service.update(created.id, archived=True)
    _add_label(db, created.id, "k1")
    _add_label(db, created.id, "k2")

    views = {v.row.slug: v for v in service.list_all()}

    assert views["cupom"].labels == 2
    assert views["cupom"].row.archived_at is not None
    assert views["real_photo"].labels == 0
    assert views["real_photo"].row.protected
