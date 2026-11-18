# SPDX-License-Identifier: GPL-3.0-only
"""Teste de arquitetura da RN-20: nenhum caminho automático leva à exclusão.

Só a rota de confirmação enfileira `delete` e só o `DeleteHandler` chama `GraphClient.delete`.
O teste varre o código-fonte (AST, sem importar) e falha quando um módulo fora da lista de
permitidos passa a referenciar a exclusão, em especial os módulos de automação
(`auto_actions`/`automation`, TF-155-06) e o consumo do runner.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src" / "odc"

# Nomes que pertencem ao caminho de exclusão confirmada.
DELETE_NAMES = frozenset(
    {
        "DeleteHandler",
        "DeletePayload",
        "ConfirmationStore",
        "enqueue_delete",
        "InvalidConfirmationError",
    }
)
DELETE_MODULES = ("odc.quarantine.delete", "odc.quarantine.confirmation")

# Quem pode referenciar o caminho de exclusão (caminhos relativos a `src/odc`).
ALLOWED_REFERENCES = frozenset(
    {
        "bootstrap.py",  # compõe o armazém de confirmações
        "jobs/factories.py",  # fábrica do handler `delete`
        "jobs/handler.py",  # `HandlerDeps.confirmations`
        "jobs/payloads.py",  # schema do payload
        "jobs/queue.py",  # `has_pending_action` lê payloads `delete`
        "jobs/runner.py",  # `delete` roda no worker de ações
        "jobs/types.py",  # enum de tipos e prioridade
        "quarantine/confirmation.py",
        "quarantine/delete.py",
        "web/services.py",  # `AppServices.confirmations`
    }
)
ALLOWED_PREFIXES = ("web/routes/",)  # a rota de confirmação é a única que enfileira `delete`
GRAPH_DELETE_CALLERS = frozenset(
    {
        "quarantine/delete.py",  # DELETE com If-Match, sempre para a lixeira
        "auth/service.py",  # `keyring.delete_password` no logout (não é a Graph)
    }
)
AUTOMATION_GLOBS = ("quarantine/auto*.py", "jobs/followups.py", "jobs/resumer.py")


def _sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _relative(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def _references_delete(tree: ast.AST) -> bool:
    """O módulo importa, nomeia ou enfileira o caminho de exclusão confirmada."""
    for node in ast.walk(tree):
        match node:
            case ast.Name(id=name) | ast.Attribute(attr=name) if name in DELETE_NAMES:
                return True
            case ast.Attribute(value=ast.Name(id="JobType"), attr="DELETE"):
                return True
            case ast.ImportFrom(module=module) | ast.Import(names=[ast.alias(name=module)]):
                if module in DELETE_MODULES:
                    return True
            case ast.alias(name=name) if name in DELETE_NAMES:
                return True
    return False


def _calls_delete(tree: ast.AST) -> bool:
    """O módulo chama algum método `.delete(...)`."""
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "delete"
        for node in ast.walk(tree)
    )


def _mentions_delete_or_confirmation(tree: ast.AST) -> bool:
    """Para módulos de automação: qualquer menção a exclusão/confirmação, mesmo indireta."""
    for node in ast.walk(tree):
        text: str | None = None
        match node:
            case ast.Name(id=text) | ast.Attribute(attr=text) | ast.alias(name=text):
                pass
            case ast.ImportFrom(module=text) | ast.Constant(value=str(text)):
                pass
        if text is not None and any(word in text.lower() for word in ("delete", "confirmation")):
            return True
    return False


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_so_a_rota_de_confirmacao_referencia_o_caminho_de_exclusao() -> None:
    offenders = [
        _relative(path)
        for path in _sources()
        if _references_delete(_tree(path))
        and _relative(path) not in ALLOWED_REFERENCES
        and not _relative(path).startswith(ALLOWED_PREFIXES)
    ]

    assert offenders == []


def test_so_o_delete_handler_chama_delete_na_graph() -> None:
    offenders = [
        _relative(path)
        for path in _sources()
        if _calls_delete(_tree(path)) and _relative(path) not in GRAPH_DELETE_CALLERS
    ]

    assert offenders == []


def test_modulos_de_automacao_nao_tem_caminho_para_a_exclusao() -> None:
    automation = sorted({path for pattern in AUTOMATION_GLOBS for path in SRC.glob(pattern)})

    offenders = [
        _relative(path) for path in automation if _mentions_delete_or_confirmation(_tree(path))
    ]

    assert offenders == []


def test_lista_de_permitidos_nao_inclui_modulos_de_automacao() -> None:
    names = ALLOWED_REFERENCES | GRAPH_DELETE_CALLERS

    assert not [name for name in names if "auto" in Path(name).name]


@pytest.mark.parametrize(
    "source",
    [
        "from odc.quarantine.delete import DeleteHandler",
        "import odc.quarantine.confirmation",
        "from odc.jobs.payloads import DeletePayload",
        "queue.enqueue(JobType.DELETE, payload)",
        "queue.enqueue_delete(ids)",
    ],
)
def test_detector_reconhece_o_caminho_de_exclusao(source: str) -> None:
    assert _references_delete(ast.parse(source))


@pytest.mark.parametrize(
    "source", ["await graph.delete(item_id, etag)", "keyring.delete_password(a, b)"]
)
def test_detector_reconhece_chamadas_delete(source: str) -> None:
    assert _calls_delete(ast.parse(source)) is source.startswith("await graph")


@pytest.mark.parametrize(
    "source",
    [
        "JobType.DELETE",
        "from odc.quarantine.confirmation import ConfirmationStore",
        "x = 'delete'",
        "graph.delete(a, b)",
    ],
)
def test_detector_de_automacao_pega_mencoes_diretas_e_indiretas(source: str) -> None:
    assert _mentions_delete_or_confirmation(ast.parse(source))


@pytest.mark.parametrize(
    "source", ["queue.enqueue_move(ids, 'auto')", "from odc.quarantine.move import MoveHandler"]
)
def test_detector_nao_acusa_codigo_de_mover(source: str) -> None:
    tree = ast.parse(source)

    assert not _references_delete(tree)
    assert not _mentions_delete_or_confirmation(tree)
