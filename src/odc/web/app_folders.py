# SPDX-License-Identifier: GPL-3.0-only
"""Garantia das pastas da aplicação no OneDrive a partir da camada web (login e startup).

Falha da Graph ou da autenticação nunca derruba o login nem o startup: vira WARNING sem dados
sensíveis e a garantia é repetida no próximo startup ou antes da próxima ação (TF-155-03/04).
"""

import logging

from sqlalchemy.exc import SQLAlchemyError

from odc.auth.errors import AuthError
from odc.graph.errors import GraphError
from odc.web.services import AppServices

logger = logging.getLogger(__name__)


async def ensure_app_folders(services: AppServices) -> None:
    """Chama `AppFolders.ensure()` quando há conta ativa e `app_folders`; nunca levanta."""
    if services.app_folders is None or services.database.account_key is None:
        return
    try:
        await services.app_folders.ensure()
    except (GraphError, AuthError, SQLAlchemyError, ValueError, OSError) as error:
        logger.warning(
            "pastas da aplicação não garantidas", extra={"error_type": type(error).__name__}
        )
