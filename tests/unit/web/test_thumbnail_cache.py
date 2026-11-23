# SPDX-License-Identifier: GPL-3.0-only
import pytest

from odc.web.thumbnail_cache import ThumbnailCache


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_devolve_apenas_chaves_vigentes_e_descarta_as_expiradas() -> None:
    clock = Clock()
    cache = ThumbnailCache(ttl_seconds=10, clock=clock)
    cache.put_many({"a": "u1"})
    clock.now = 5
    cache.put_many({"b": "u2"})

    clock.now = 10
    assert cache.get_many(["a", "b", "c"]) == {"b": "u2"}
    assert len(cache) == 1
    clock.now = 15
    assert cache.get_many(["b"]) == {}
    assert len(cache) == 0


def test_despeja_o_menos_recentemente_usado_ao_exceder_o_limite() -> None:
    cache = ThumbnailCache(max_entries=2)
    cache.put_many({"a": "1", "b": "2"})
    cache.get_many(["a"])

    cache.put_many({"c": "3"})

    assert cache.get_many(["a", "b", "c"]) == {"a": "1", "c": "3"}


def test_discard_e_clear() -> None:
    cache = ThumbnailCache()
    cache.put_many({"a": "1", "b": "2", "c": "3"})

    cache.discard_many(["a", "inexistente"])
    assert cache.get_many(["a", "b"]) == {"b": "2"}
    cache.clear()
    assert len(cache) == 0


@pytest.mark.parametrize("kwargs", [{"ttl_seconds": 0}, {"max_entries": 0}])
def test_rejeita_parametros_nao_positivos(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="positivos"):
        ThumbnailCache(**kwargs)  # type: ignore[arg-type]
