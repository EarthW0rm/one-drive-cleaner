# SPDX-License-Identifier: GPL-3.0-only
"""Modelos das respostas do Microsoft Graph (plan 4.2).

Dados externos nunca são usados sem validação. Regra de tolerância:
- `id` ausente ou inválido → `ValidationError` (o chamador descarta o item);
- qualquer outro campo com tipo errado ou fora de faixa → `None` (tratado como ausente);
- páginas e lotes: entradas inválidas são descartadas e contadas em `dropped`.
"""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Annotated, cast

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    ValidatorFunctionWrapHandler,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

# `to_camel` gera `...Datetime`; a Graph usa `...DateTime`. Aliases explícitos para esses campos.

UNKNOWN_ERROR_CODE = "unknown"


class _GraphModel(BaseModel):  # type: ignore[explicit-any]  # BaseModel usa Any
    """Base: aliases camelCase da Graph, campos desconhecidos ignorados e instância imutável."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
        frozen=True,
    )


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int_at_least(minimum: int) -> Callable[[object], int | None]:
    def _check(value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            return None
        return value

    return _check


def _datetime_or_none(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _facet_present(value: object) -> bool:
    return isinstance(value, Mapping) or value is True


def _valid_entries(raw: object, model: type[BaseModel]) -> tuple[list[object], int]:
    """Mantém apenas entradas que validam no `model`; devolve (válidas, quantidade descartada)."""
    if not isinstance(raw, list):
        return [], 0
    kept: list[object] = []
    for entry in cast(list[object], raw):
        try:
            model.model_validate(entry)
        except ValidationError:
            continue
        kept.append(entry)
    return kept, len(cast(list[object], raw)) - len(kept)


OptionalStr = Annotated[str | None, BeforeValidator(_str_or_none)]
OptionalDateTime = Annotated[datetime | None, BeforeValidator(_datetime_or_none)]
OptionalSize = Annotated[int | None, BeforeValidator(_int_at_least(0))]
OptionalDimension = Annotated[int | None, BeforeValidator(_int_at_least(1))]


class ParentReference(_GraphModel):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    """Pasta pai do item. `path` contém nomes de pastas: nunca logar."""

    id: str = Field(min_length=1)
    path: OptionalStr = None
    drive_id: OptionalStr = None


class FileFacet(_GraphModel):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    mime_type: OptionalStr = None


class ImageFacet(_GraphModel):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    width: OptionalDimension = None
    height: OptionalDimension = None


class PhotoFacet(_GraphModel):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    camera_make: OptionalStr = None
    camera_model: OptionalStr = None
    taken_datetime: OptionalDateTime = Field(default=None, alias="takenDateTime")


class FolderFacet(_GraphModel):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    child_count: OptionalSize = None


class DriveItem(_GraphModel):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    """Item do OneDrive com os campos do `$select` da varredura (plan 4.2).

    `location_present` indica apenas a existência da faceta; coordenadas não são modeladas.
    `deleted` indica item removido em delta. Nomes de arquivo e caminhos não são logados.
    """

    id: str = Field(min_length=1)
    name: OptionalStr = None
    size: OptionalSize = None
    c_tag: OptionalStr = None
    e_tag: OptionalStr = None
    created_datetime: OptionalDateTime = Field(default=None, alias="createdDateTime")
    last_modified_datetime: OptionalDateTime = Field(default=None, alias="lastModifiedDateTime")
    parent_reference: ParentReference | None = None
    file: FileFacet | None = None
    folder: FolderFacet | None = None
    image: ImageFacet | None = None
    photo: PhotoFacet | None = None
    location_present: bool = False
    deleted: bool = False

    @model_validator(mode="before")
    @classmethod
    def _derive_flags(cls, data: object) -> object:
        if not isinstance(data, Mapping):
            return data
        raw = cast(Mapping[str, object], data)
        return {
            **raw,
            "location_present": isinstance(raw.get("location"), Mapping),
            "deleted": _facet_present(raw.get("deleted")),
        }

    @field_validator("parent_reference", "file", "folder", "image", "photo", mode="wrap")
    @classmethod
    def _facet_or_none(cls, value: object, handler: ValidatorFunctionWrapHandler) -> object:
        try:
            return handler(value)
        except ValidationError:
            return None


class DeltaPage(_GraphModel):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    """Página da delta: `value` com itens válidos, `@odata.nextLink` ou `@odata.deltaLink`."""

    items: tuple[DriveItem, ...] = Field(default=(), alias="value")
    next_link: str | None = Field(default=None, alias="@odata.nextLink")
    delta_link: str | None = Field(default=None, alias="@odata.deltaLink")
    dropped: int = 0

    @model_validator(mode="before")
    @classmethod
    def _keep_valid_items(cls, data: object) -> object:
        if not isinstance(data, Mapping):
            return data
        raw = cast(Mapping[str, object], data)
        kept, dropped = _valid_entries(raw.get("value"), DriveItem)
        return {**raw, "value": kept, "dropped": dropped}


class Drive(_GraphModel):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    id: str = Field(min_length=1)
    drive_type: OptionalStr = None


class ThumbnailUrl(_GraphModel):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    """URL pré-autenticada de miniatura. Oculta do `repr` por conter token de acesso."""

    url: str = Field(min_length=1, pattern=r"^https://", repr=False)


class BatchItem(_GraphModel):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    id: str = Field(min_length=1)
    status: StrictInt
    body: dict[str, object] | None = None

    @field_validator("body", mode="before")
    @classmethod
    def _body_or_none(cls, value: object) -> dict[str, object] | None:
        if isinstance(value, dict):
            return cast(dict[str, object], value)
        return None


class BatchResponse(_GraphModel):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    """Resposta do `$batch`. Entradas sem `id` ou `status` válidos são descartadas."""

    responses: tuple[BatchItem, ...] = ()
    dropped: int = 0

    @model_validator(mode="before")
    @classmethod
    def _keep_valid_responses(cls, data: object) -> object:
        if not isinstance(data, Mapping):
            return data
        raw = cast(Mapping[str, object], data)
        kept, dropped = _valid_entries(raw.get("responses"), BatchItem)
        return {**raw, "responses": kept, "dropped": dropped}


class GraphErrorBody(_GraphModel):  # type: ignore[explicit-any]  # herda de BaseModel (usa Any)
    """Erro da Graph (`{"error": {code, message}}`). Corpo fora desse formato → código `unknown`."""

    code: str = UNKNOWN_ERROR_CODE
    message: str = ""

    @classmethod
    def from_payload(cls, payload: object) -> "GraphErrorBody":
        inner = payload.get("error") if isinstance(payload, Mapping) else None
        if not isinstance(inner, Mapping):
            return cls()
        fields = cast(Mapping[str, object], inner)
        code = fields.get("code")
        message = fields.get("message")
        return cls(
            code=code if isinstance(code, str) and code else UNKNOWN_ERROR_CODE,
            message=message if isinstance(message, str) else "",
        )
