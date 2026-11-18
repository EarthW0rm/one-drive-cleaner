# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path

from sqlalchemy import create_engine

from odc.storage.account_db import AccountDatabase
from odc.web import services as services_module
from odc.web.services import AppServices, LoginRequiredGateway, default_static_dir


def test_placeholder_informa_login_necessario_sem_conta() -> None:
    gateway = LoginRequiredGateway()

    assert gateway.auth_state() == "login_required"
    assert gateway.display_name() is None


def test_static_dir_padrao_fica_dentro_do_pacote_web() -> None:
    expected = Path(services_module.__file__).resolve().parent / "static"

    assert default_static_dir() == expected


def test_segredo_de_sessao_e_aleatorio_por_instancia_e_fora_do_repr() -> None:
    database = AccountDatabase(create_engine("sqlite://"), Path("odc.db"))

    first = AppServices(database=database)
    second = AppServices(database=database)

    assert first.session_secret != second.session_secret
    assert len(first.session_secret) >= 32
    assert first.session_secret not in repr(first)
