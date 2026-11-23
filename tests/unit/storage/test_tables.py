# SPDX-License-Identifier: GPL-3.0-only
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError

from odc.storage import tables
from odc.storage.engine import create_engine_for
from odc.storage.migrate import upgrade

NOW = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_path = tmp_path / "odc.db"
    upgrade(db_path)
    eng = create_engine_for(db_path)
    yield eng
    eng.dispose()


@pytest.fixture
def conn(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as connection:
        yield connection


def _insert_root(conn: Connection) -> None:
    conn.execute(
        text("INSERT INTO scan_root (id, drive_item_id, display_path) VALUES (1, 'r1', '/a')")
    )


def _insert_item(conn: Connection, item_id: int = 1, state: str = "DESCOBERTO") -> None:
    conn.execute(
        text(
            "INSERT INTO item (id, drive_item_id, scan_root_id, parent_id, name, mime_type,"
            " size_bytes, remote_created_at, remote_modified_at, ctag, etag, state,"
            " created_at, updated_at) VALUES (:id, :d, 1, 'p', 'n', 'image/png', 1, :t, :t,"
            " 'c', 'e', :s, :t, :t)"
        ),
        {"id": item_id, "d": f"d{item_id}", "t": NOW, "s": state},
    )


def _insert_embedding_model(conn: Connection) -> None:
    conn.execute(
        text(
            "INSERT OR REPLACE INTO embedding_model (id, name, dim, thumb_size, is_active)"
            " VALUES (1, 'm', 512, 'c384x384', 1)"
        )
    )


def _insert_model_version(conn: Connection, status: str) -> None:
    conn.execute(
        text(
            "INSERT INTO model_version (strategy, embedding_model_id, status, labels_count,"
            " categories, params, trigger, created_at)"
            " VALUES ('zero_shot', 1, :s, 0, '[]', x'00', 'bootstrap', :t)"
        ),
        {"s": status, "t": NOW},
    )


def test_cria_as_13_tabelas_do_plan(conn: Connection) -> None:
    names = set(
        conn.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                " AND name != 'alembic_version'"
            )
        ).scalars()
    )

    assert names == set(tables.metadata.tables)
    assert len(names) == 13


def test_categoria_protegida_e_removivel_falha(conn: Connection) -> None:
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO category (slug, name, description, zero_shot_prompts, removable,"
                " protected, created_at) VALUES ('x', 'x', 'x', '[]', 1, 1, :t)"
            ),
            {"t": NOW},
        )


def test_categoria_protegida_nao_pode_ser_arquivada(conn: Connection) -> None:
    with pytest.raises(IntegrityError):
        conn.execute(
            text("UPDATE category SET archived_at = :t WHERE slug = 'real_photo'"), {"t": NOW}
        )


def test_categoria_removivel_pode_ser_arquivada(conn: Connection) -> None:
    conn.execute(text("UPDATE category SET archived_at = :t WHERE slug = 'screenshot'"), {"t": NOW})


def test_dois_model_version_current_falham(conn: Connection) -> None:
    _insert_embedding_model(conn)
    _insert_model_version(conn, "current")

    with pytest.raises(IntegrityError):
        _insert_model_version(conn, "current")


def test_varios_model_version_nao_current_sao_permitidos(conn: Connection) -> None:
    _insert_embedding_model(conn)
    _insert_model_version(conn, "current")
    _insert_model_version(conn, "candidate")
    _insert_model_version(conn, "candidate")
    _insert_model_version(conn, "retired")


def test_label_orfao_falha_por_fk(conn: Connection) -> None:
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO label (item_id, category_id, source, created_at)"
                " VALUES (999, 1, 'review', :t)"
            ),
            {"t": NOW},
        )


def test_estado_de_item_invalido_falha(conn: Connection) -> None:
    _insert_root(conn)

    with pytest.raises(IntegrityError):
        _insert_item(conn, state="INVALIDO")


def test_estado_de_item_valido_e_aceito(conn: Connection) -> None:
    _insert_root(conn)
    _insert_item(conn, state="REMOVIDO_EXTERNAMENTE")


def test_failure_reason_invalido_falha(conn: Connection) -> None:
    _insert_root(conn)
    _insert_item(conn)

    with pytest.raises(IntegrityError):
        conn.execute(text("UPDATE item SET failure_reason = 'nope' WHERE id = 1"))


def test_failure_reason_nulo_e_valido_sao_aceitos(conn: Connection) -> None:
    _insert_root(conn)
    _insert_item(conn)
    conn.execute(text("UPDATE item SET failure_reason = 'decode_error' WHERE id = 1"))
    conn.execute(text("UPDATE item SET failure_reason = NULL WHERE id = 1"))


def test_flag_fora_de_0_1_falha(conn: Connection) -> None:
    with pytest.raises(IntegrityError):
        conn.execute(
            text("INSERT INTO scan_root (drive_item_id, display_path, enabled) VALUES ('a','b',2)")
        )


def test_confidence_fora_de_0_1_falha(conn: Connection) -> None:
    _insert_root(conn)
    _insert_item(conn)
    _insert_embedding_model(conn)
    _insert_model_version(conn, "candidate")

    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO prediction (item_id, model_version_id, category_id, confidence,"
                " margin, predicted_at) VALUES (1, 1, 1, 1.5, 0.1, :t)"
            ),
            {"t": NOW},
        )


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO job (type, payload, status, created_at) VALUES ('x', '{}', 'queued', 't')",
        "INSERT INTO job (type, payload, status, created_at) VALUES ('scan', '{}', 'x', 't')",
        "INSERT INTO job (type, payload, status, pause_reason, created_at)"
        " VALUES ('scan', '{}', 'paused', 'x', 't')",
        "INSERT INTO journal (op_id, operation, phase, created_at, updated_at)"
        " VALUES ('o', 'x', 'intent', 't', 't')",
        "INSERT INTO journal (op_id, operation, phase, created_at, updated_at)"
        " VALUES ('o', 'move', 'x', 't', 't')",
        "INSERT INTO embedding_model (name, dim, thumb_size, is_active) VALUES ('m', 0, 's', 1)",
    ],
)
def test_enums_invalidos_de_job_e_journal_falham(conn: Connection, statement: str) -> None:
    with pytest.raises(IntegrityError):
        conn.execute(text(statement))


def test_job_valido_usa_defaults(conn: Connection) -> None:
    conn.execute(
        text(
            "INSERT INTO job (type, payload, status, created_at) VALUES ('scan','{}','queued','t')"
        )
    )

    row = conn.execute(text("SELECT priority, attempts FROM job")).one()

    assert tuple(row) == (0, 0)


def test_op_id_do_journal_e_unico(conn: Connection) -> None:
    stmt = text(
        "INSERT INTO journal (op_id, operation, phase, created_at, updated_at)"
        " VALUES ('o', 'move', 'intent', 't', 't')"
    )
    conn.execute(stmt)

    with pytest.raises(IntegrityError):
        conn.execute(stmt)


def test_drive_item_id_do_item_e_unico(conn: Connection) -> None:
    _insert_root(conn)
    _insert_item(conn, item_id=1)

    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO item (id, drive_item_id, scan_root_id, parent_id, name, mime_type,"
                " size_bytes, remote_created_at, remote_modified_at, ctag, etag, state,"
                " created_at, updated_at) VALUES (2, 'd1', 1, 'p', 'n', 'image/png', 1, 't', 't',"
                " 'c', 'e', 'DESCOBERTO', 't', 't')"
            )
        )


def test_quarantine_entry_com_enums_invalidos_falha(conn: Connection) -> None:
    _insert_root(conn)
    _insert_item(conn)

    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO quarantine_entry (item_id, original_parent_id, original_parent_path,"
                " original_name, quarantined_name, mode, status)"
                " VALUES (1, 'p', '/p', 'n', 'n', 'manual', 'x')"
            )
        )


def test_seed_de_categorias(conn: Connection) -> None:
    rows = conn.execute(
        text("SELECT slug, name, removable, protected, zero_shot_prompts FROM category ORDER BY id")
    ).all()

    assert [tuple(r)[:4] for r in rows] == [
        ("screenshot", "Print de tela", 1, 0),
        ("real_photo", "Foto real", 0, 1),
        ("meme_sticker", "Meme/figurinha", 1, 0),
        ("document_receipt", "Documento/recibo", 1, 0),
    ]
    assert all(isinstance(json.loads(r[4]), list) and json.loads(r[4]) for r in rows)


def test_seed_de_settings(conn: Connection) -> None:
    rows = conn.execute(text("SELECT key, value FROM setting")).all()

    values = {k: json.loads(v) for k, v in rows}
    assert values == {
        "automation_mode": "evolutive",
        "auto_threshold": 0.98,
        "retrain_every": 20,
        "gate_precision": 0.98,
        "gate_min_labels": 200,
        "gate_min_support": 30,
        "temp_cap_bytes": 2147483648,
        "otel_enabled": True,
        "otel_endpoint": "http://127.0.0.1:4317",
    }


def test_embedding_model_tem_o_seed_do_benchmark(conn: Connection) -> None:
    rows = conn.execute(text("SELECT name, dim, thumb_size, is_active FROM embedding_model")).all()

    assert [tuple(r) for r in rows] == [("ViT-B-32/laion2b_s34b_b79k", 512, "c384x384", 1)]


def test_indices_do_plan_3_2_existem(conn: Connection) -> None:
    names = set(conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'")).scalars())

    assert {
        "ux_item_drive_id",
        "ix_item_state",
        "ix_item_root_state",
        "ix_label_item_created",
        "ix_pred_cat_margin",
        "ix_pred_cat_conf",
        "ux_model_current",
        "ix_qe_status",
        "ix_journal_phase",
        "ix_job_claim",
    } <= names


def test_in_check_aceita_nulo_quando_solicitado() -> None:
    assert tables.in_check("c", ("a", "b")) == "c IN ('a', 'b')"
    assert tables.in_check("c", ("a",), nullable=True) == "c IS NULL OR c IN ('a')"
