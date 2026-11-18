# SPDX-License-Identifier: GPL-3.0-only
"""Varredura completa com marcação de "vistos" e reconciliação de escopo (plan 2.4, 5.1).

Uma passada completa (primeira varredura, modo `full` ou ressincronização após 410) começa
descartando os links da pasta e registrando o instante de início. Toda imagem vista é regravada
com `updated_at` posterior a esse instante; ao final, os itens da raiz em estado de acervo que não
foram tocados desde o início não existem mais no escopo → `REMOVIDO_EXTERNAMENTE`. A marcação é
persistida no próprio item, então sobrevive a interrupções sem guardar listas de IDs.

Erros só deixam de remover (nunca removem a mais): qualquer outra escrita no item depois do
início também o conta como visto. Itens com operação do app em andamento não são tocados.
Nenhuma linha de `item` é apagada (RN-26).
"""

from collections.abc import Sequence

from sqlalchemy.orm import Session

from odc.scanning.changes import CATALOG_STATES, REMOVIDO_EXTERNAMENTE
from odc.storage.repositories import items, journal, scan_roots

_UNSEEN_STATES = tuple(sorted(CATALOG_STATES))


def start_full_pass(session: Session, root_id: int) -> None:
    """Descarta `next_link`/`delta_link`: a enumeração recomeça do início da pasta."""
    scan_roots.clear_links(session, root_id)


def mark_removed(session: Session, item_ids: Sequence[int], *, now: str) -> int:
    """`REMOVIDO_EXTERNAMENTE` para os itens sem operação do app pendente; devolve quantos."""
    pending = journal.pending_item_ids(session, item_ids)
    changed = 0
    for item_id in item_ids:
        if item_id in pending:
            continue
        items.set_state(session, item_id, REMOVIDO_EXTERNAMENTE, now=now)
        changed += 1
    return changed


def finish_full_pass(session: Session, root_id: int, *, started_at: str, now: str) -> int:
    """Fecha a passada completa: itens da raiz não vistos saem do acervo; marca `last_full_scan_at`.

    Deve rodar na mesma transação que grava o `delta_link` final. Devolve quantos itens saíram.
    """
    unseen = items.list_ids_in_roots(session, [root_id], _UNSEEN_STATES, updated_before=started_at)
    removed = mark_removed(session, unseen, now=now)
    scan_roots.mark_full_scan(session, root_id, now=now)
    return removed


def reconcile_out_of_scope(session: Session, *, now: str) -> int:
    """Itens que ficaram em raízes desabilitadas (desmarcadas) saem do acervo.

    Só age quando todas as raízes habilitadas já concluíram ao menos uma varredura (têm
    `delta_link` e nenhuma paginação pendente): só então uma raiz ancestral recém-escolhida já
    adotou os itens da descendente que ela substituiu. Devolve quantos itens saíram.
    """
    roots = scan_roots.list_roots(session)
    enabled = [root for root in roots if root.enabled]
    if not enabled or any(r.delta_link is None or r.next_link is not None for r in enabled):
        return 0
    disabled = [root.id for root in roots if not root.enabled]
    stale = items.list_ids_in_roots(session, disabled, _UNSEEN_STATES)
    return mark_removed(session, stale, now=now)


__all__ = ["finish_full_pass", "mark_removed", "reconcile_out_of_scope", "start_full_pass"]
