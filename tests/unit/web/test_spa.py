# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from odc.config import AppSettings
from odc.web.app import create_app
from odc.web.services import AppServices
from odc.web.spa import IMMUTABLE_CACHE, REVALIDATE_CACHE

from .conftest import ASSET_JS, BASE_URL, INDEX_HTML


@pytest.mark.parametrize("path", ["/", "/review", "/quarantine", "/categories", "/scans"])
def test_rotas_do_cliente_devolvem_index_html(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.text == INDEX_HTML
    assert response.headers["Cache-Control"] == REVALIDATE_CACHE


def test_rota_desconhecida_nao_api_devolve_index_html(client: TestClient) -> None:
    response = client.get("/rota/que/nao/existe")

    assert response.status_code == 200
    assert response.text == INDEX_HTML


def test_asset_com_hash_tem_cache_imutavel(client: TestClient) -> None:
    response = client.get("/assets/index-abc123.js")

    assert response.status_code == 200
    assert response.text == ASSET_JS
    assert response.headers["Cache-Control"] == IMMUTABLE_CACHE


def test_asset_inexistente_retorna_404_padrao_em_vez_de_index(client: TestClient) -> None:
    response = client.get("/assets/ausente-999.js")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_arquivo_estatico_fora_de_assets_revalida(client: TestClient) -> None:
    response = client.get("/favicon.svg")

    assert response.status_code == 200
    assert response.text == "<svg/>"
    assert response.headers["Cache-Control"] == REVALIDATE_CACHE


def test_head_e_permitido_na_spa(client: TestClient) -> None:
    assert client.head("/review").status_code == 200


def test_api_inexistente_retorna_404_no_formato_padrao(client: TestClient) -> None:
    response = client.get("/api/inexistente")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "NOT_FOUND", "message": "Recurso não encontrado.", "details": None}
    }


def test_auth_desconhecido_retorna_404_padrao(client: TestClient) -> None:
    response = client.get("/auth/inexistente")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_traversal_nao_serve_arquivos_fora_do_diretorio_estatico(
    client: TestClient, static_dir: Path
) -> None:
    (static_dir.parent / "segredo.txt").write_text("segredo", encoding="utf-8")

    for path in ("/../segredo.txt", "/%2e%2e/segredo.txt", "/assets/../../segredo.txt"):
        response = client.get(path)
        assert response.text in (INDEX_HTML, "") or response.status_code in (400, 404)
        assert "segredo" not in response.text


def test_byte_nulo_no_caminho_cai_no_index(client: TestClient) -> None:
    response = client.get("/arquivo%00.js")

    assert response.status_code == 200
    assert response.text == INDEX_HTML


def test_diretorio_nao_e_servido_como_arquivo(client: TestClient) -> None:
    response = client.get("/assets")

    assert response.text == INDEX_HTML


def test_symlink_para_fora_do_diretorio_e_bloqueado(client: TestClient, static_dir: Path) -> None:
    outside = static_dir.parent / "fora.txt"
    outside.write_text("conteudo-externo", encoding="utf-8")
    (static_dir / "link.txt").symlink_to(outside)

    response = client.get("/link.txt")

    assert "conteudo-externo" not in response.text


def test_sem_build_da_spa_retorna_404_padrao(services: AppServices, tmp_path: Path) -> None:
    empty = AppServices(
        database=services.database, auth=services.auth, static_dir=tmp_path / "vazio"
    )
    app = create_app(AppSettings.model_construct(), empty)
    with TestClient(app, base_url=BASE_URL) as client:
        response = client.get("/")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
