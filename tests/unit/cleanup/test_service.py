# SPDX-License-Identifier: GPL-3.0-only
import os
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from odc.cleanup import service as service_module
from odc.cleanup.service import CleanupService
from odc.storage.repositories import jobs, settings
from odc.storage.uow import UnitOfWork
from tests.unit.cleanup.conftest import Dirs, EngineHolder

NOW = "2026-11-17T10:00:00+00:00"


def _write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


class TestPurgeTemp:
    def test_remove_arquivos_e_subpastas_e_devolve_os_bytes(
        self, service: CleanupService, dirs: Dirs
    ) -> None:
        _write(dirs.temp / "a.bin", 100)
        _write(dirs.temp / "sub" / "deep" / "b.bin", 250)
        (dirs.temp / "vazia").mkdir()

        freed = service.purge_temp()

        assert freed == 350
        assert dirs.temp.is_dir()
        assert list(dirs.temp.iterdir()) == []

    def test_temporario_vazio_libera_zero(self, service: CleanupService) -> None:
        assert service.purge_temp() == 0

    def test_symlink_para_arquivo_externo_nao_e_removido(
        self, service: CleanupService, dirs: Dirs, tmp_path: Path
    ) -> None:
        outside = _write(tmp_path / "fora" / "segredo.txt", 10)
        (dirs.temp / "link").symlink_to(outside)
        _write(dirs.temp / "ok.bin", 5)

        freed = service.purge_temp()

        assert freed == 5
        assert outside.read_bytes() == b"x" * 10
        assert (dirs.temp / "link").is_symlink()

    def test_symlink_para_diretorio_externo_nao_e_seguido(
        self, service: CleanupService, dirs: Dirs, tmp_path: Path
    ) -> None:
        outside_dir = tmp_path / "fora"
        victim = _write(outside_dir / "importante.txt", 10)
        (dirs.temp / "dirlink").symlink_to(outside_dir, target_is_directory=True)

        assert service.purge_temp() == 0
        assert victim.exists()
        assert (dirs.temp / "dirlink").is_symlink()

    def test_pasta_nao_vazia_por_symlink_permanece(
        self, service: CleanupService, dirs: Dirs, tmp_path: Path
    ) -> None:
        outside = _write(tmp_path / "fora" / "f.txt", 1)
        (dirs.temp / "sub").mkdir()
        (dirs.temp / "sub" / "link").symlink_to(outside)
        _write(dirs.temp / "sub" / "x.bin", 3)

        assert service.purge_temp() == 3
        assert (dirs.temp / "sub" / "link").is_symlink()
        assert outside.exists()

    def test_arquivo_que_nao_pode_ser_removido_e_ignorado(
        self, service: CleanupService, dirs: Dirs, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        busy = _write(dirs.temp / "em_uso.bin", 7)
        _write(dirs.temp / "livre.bin", 3)
        original = Path.unlink

        def fake_unlink(self: Path, missing_ok: bool = False) -> None:
            if self == busy:
                raise PermissionError
            original(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", fake_unlink)

        assert service.purge_temp() == 3
        assert busy.exists()

    def test_diretorio_ilegivel_e_ignorado(
        self, service: CleanupService, dirs: Dirs, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(dirs.temp / "sub" / "a.bin", 4)
        real_scandir = os.scandir

        def fake_scandir(path: str | os.PathLike[str]) -> object:
            if Path(path).name == "sub":
                raise PermissionError
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", fake_scandir)

        assert service.purge_temp() == 0
        monkeypatch.undo()
        assert (dirs.temp / "sub" / "a.bin").read_bytes() == b"x" * 4

    def test_alvo_fora_do_temporario_e_recusado(
        self, service: CleanupService, dirs: Dirs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        victim = _write(tmp_path / "fora" / "v.txt", 2)
        monkeypatch.setattr("odc.cleanup.service._is_inside", lambda _root, _path: False)
        _write(dirs.temp / "a.bin", 1)

        assert service.purge_temp() == 0
        assert victim.exists()
        assert (dirs.temp / "a.bin").exists()


class TestIsInside:
    def test_erro_ao_resolver_conta_como_fora(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(self: Path, strict: bool = False) -> Path:
            raise OSError

        monkeypatch.setattr(Path, "resolve", boom)

        assert service_module._is_inside(Path("/a"), Path("/a/b")) is False


class TestUsage:
    def test_disk_usage_soma_wal_e_shm_no_db(self, service: CleanupService, dirs: Dirs) -> None:
        wal = _write(Path(f"{dirs.db}-wal"), 1000)
        shm = _write(Path(f"{dirs.db}-shm"), 500)
        expected = dirs.db.stat().st_size + wal.stat().st_size + shm.stat().st_size

        assert service.disk_usage()["db"] == expected
        assert expected >= dirs.db.stat().st_size + 1500

    def test_disk_usage_por_componente(self, service: CleanupService, dirs: Dirs) -> None:
        _write(dirs.temp / "a" / "t.bin", 11)
        _write(dirs.models / "m.bin", 22)
        _write(dirs.logs / "odc.log", 33)

        usage = service.disk_usage()

        assert (usage["temp"], usage["model"], usage["logs"]) == (11, 22, 33)
        assert set(usage) == {"temp", "db", "model", "logs"}

    def test_symlink_nao_conta_no_uso(
        self, service: CleanupService, dirs: Dirs, tmp_path: Path
    ) -> None:
        outside = _write(tmp_path / "fora" / "grande.bin", 999)
        (dirs.temp / "link").symlink_to(outside)

        assert service.temp_usage() == 0

    def test_banco_em_memoria_conta_zero(self, dirs: Dirs) -> None:
        memory = EngineHolder(create_engine("sqlite://"))
        svc = CleanupService(
            memory,
            temp_root=lambda: dirs.temp,
            models_root=lambda: dirs.models,
            logs_root=lambda: dirs.logs,
        )

        assert svc.disk_usage()["db"] == 0

    def test_diretorio_inexistente_conta_zero(self, holder: EngineHolder, tmp_path: Path) -> None:
        svc = CleanupService(
            holder,
            temp_root=lambda: tmp_path / "nada",
            models_root=lambda: tmp_path / "nada",
            logs_root=lambda: tmp_path / "nada",
        )

        assert svc.temp_usage() == 0


class TestTempCap:
    def test_teto_padrao_e_2_gib(self, service: CleanupService) -> None:
        assert service.temp_cap_bytes() == 2 * 1024**3

    def test_abaixo_do_teto_nao_atinge(self, service: CleanupService, dirs: Dirs) -> None:
        _write(dirs.temp / "a.bin", 10)

        assert service.temp_cap_reached() is False

    def test_no_teto_atinge(
        self, service: CleanupService, holder: EngineHolder, dirs: Dirs
    ) -> None:
        with UnitOfWork(holder.engine) as session:
            settings.set_value(session, "temp_cap_bytes", 10, now=NOW)
        _write(dirs.temp / "a.bin", 10)

        assert service.temp_cap_reached() is True
        service.purge_temp()
        assert service.temp_cap_reached() is False


def _fragment(db: Path) -> None:
    """Cria páginas livres: insere e apaga muitas linhas de `app_meta`."""
    connection = sqlite3.connect(db)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS _lixo (v BLOB)")
        connection.executemany("INSERT INTO _lixo VALUES (?)", [(b"x" * 4000,)] * 200)
        connection.commit()
        connection.execute("DROP TABLE _lixo")
        connection.commit()
    finally:
        connection.close()


class TestVacuum:
    def test_sem_fragmentacao_nao_executa(self, service: CleanupService) -> None:
        assert service.maybe_vacuum() is False

    def test_com_mais_de_20_por_cento_livre_executa_e_encolhe(
        self, service: CleanupService, dirs: Dirs
    ) -> None:
        _fragment(dirs.db)
        before = service.disk_usage()["db"]

        assert service.maybe_vacuum() is True

        assert service.disk_usage()["db"] < before
        assert service.maybe_vacuum() is False

    def test_adia_com_outro_job_rodando(
        self, service: CleanupService, holder: EngineHolder, dirs: Dirs
    ) -> None:
        _fragment(dirs.db)
        with UnitOfWork(holder.engine) as session:
            jobs.enqueue(session, job_type="scan", payload={"mode": "auto"}, now=NOW)
            assert jobs.claim_next(session, now=NOW, types=["scan"]) is not None

        assert service.maybe_vacuum() is False

    def test_job_de_limpeza_rodando_nao_bloqueia(
        self, service: CleanupService, holder: EngineHolder, dirs: Dirs
    ) -> None:
        _fragment(dirs.db)
        with UnitOfWork(holder.engine) as session:
            jobs.enqueue(session, job_type="cleanup", payload={"trigger": "job_end"}, now=NOW)
            assert jobs.claim_next(session, now=NOW, types=["cleanup"]) is not None

        assert service.maybe_vacuum() is True

    def test_falha_do_sqlite_adia(
        self,
        service: CleanupService,
        holder: EngineHolder,
        dirs: Dirs,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _fragment(dirs.db)

        real = holder.engine.raw_connection
        calls: list[int] = []

        def boom() -> object:
            calls.append(1)
            if len(calls) > 1:  # a 1ª conexão é a leitura dos pragmas; a 2ª é o VACUUM
                raise sqlite3.OperationalError("database or disk is full")
            return real()

        monkeypatch.setattr(holder.engine, "raw_connection", boom)

        assert service.maybe_vacuum() is False


def test_diretorios_padrao_vem_de_odc_paths(
    holder: EngineHolder, dirs: Dirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("odc.paths.temp_dir", lambda: dirs.temp)
    monkeypatch.setattr("odc.paths.models_dir", lambda: dirs.models)
    monkeypatch.setattr("odc.paths.logs_dir", lambda: dirs.logs)
    _write(dirs.models / "m.bin", 2)
    _write(dirs.logs / "l.log", 3)

    usage = CleanupService(holder).disk_usage()

    assert (usage["model"], usage["logs"], usage["temp"]) == (2, 3, 0)
