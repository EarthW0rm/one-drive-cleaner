# SPDX-License-Identifier: GPL-3.0-only
"""Fronteira tipada com o MSAL (biblioteca sem tipagem): isola o `PublicClientApplication`."""

from collections.abc import Callable, Mapping
from typing import Protocol

from msal import PublicClientApplication, SerializableTokenCache

type Flow = dict[str, object]
type AccountInfo = Mapping[str, object]
type TokenResult = Mapping[str, object]


class MsalClient(Protocol):
    """Subconjunto do `PublicClientApplication` usado pelo serviço de autenticação."""

    def initiate_auth_code_flow(
        self, scopes: list[str], redirect_uri: str | None = None, prompt: str | None = None
    ) -> Flow: ...

    def acquire_token_by_auth_code_flow(
        self, auth_code_flow: Flow, auth_response: dict[str, str]
    ) -> TokenResult: ...

    def get_accounts(self) -> list[AccountInfo]: ...

    def acquire_token_silent_with_error(
        self, scopes: list[str], account: AccountInfo, *, force_refresh: bool = False
    ) -> TokenResult | None: ...


type MsalFactory = Callable[[str, str, SerializableTokenCache], MsalClient]


def default_app_factory(
    client_id: str, authority: str, token_cache: SerializableTokenCache
) -> MsalClient:
    """Public client (sem secret): auth code + PKCE, authority `consumers`."""
    client: MsalClient = PublicClientApplication(
        client_id, authority=authority, token_cache=token_cache
    )
    return client
