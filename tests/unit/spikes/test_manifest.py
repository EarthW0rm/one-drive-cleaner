# SPDX-License-Identifier: GPL-3.0-only
import math
from pathlib import Path

import pytest
from pydantic import ValidationError
from spikes.embedding_benchmark import manifest as mf
from spikes.embedding_benchmark.errors import BenchmarkError


def _item(**overrides: object) -> mf.ManifestItem:
    base: dict[str, object] = {"seq": "000001", "drive_item_id": "FAKE-1"}
    return mf.ManifestItem.model_validate({**base, **overrides})


def test_format_seq_usa_seis_digitos() -> None:
    assert mf.format_seq(7) == "000007"
    assert mf.format_seq(123456) == "123456"


def test_save_e_load_fazem_ida_e_volta_com_permissao_restrita(tmp_path: Path) -> None:
    manifest = mf.Manifest(
        seed=42,
        folder="camera_roll",
        custom_size_supported=False,
        items=(_item(name_flags=("screenshot",), sizes=("large",), suggested="screenshot"),),
    )

    mf.save_manifest(tmp_path, manifest)

    assert mf.load_manifest(tmp_path) == manifest
    assert mf.manifest_path(tmp_path).stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp"))


def test_load_manifest_ausente_devolve_none(tmp_path: Path) -> None:
    assert mf.load_manifest(tmp_path) is None


@pytest.mark.parametrize(
    "content",
    ["", "{", '{"seed": "x"}', '{"seed": 1, "folder": "f", "items": [{"seq": "1"}]}'],
)
def test_load_manifest_invalido_levanta_erro_do_benchmark(tmp_path: Path, content: str) -> None:
    mf.manifest_path(tmp_path).write_text(content, encoding="utf-8")

    with pytest.raises(BenchmarkError, match=r"manifest\.local\.json"):
        mf.load_manifest(tmp_path)


@pytest.mark.parametrize(
    "overrides",
    [{"seq": "12"}, {"seq": "abc123"}, {"width": 0}, {"suggested": "meme"}, {"nome": "foto.jpg"}],
)
def test_item_rejeita_valores_fora_do_contrato(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _item(**overrides)


def test_manifest_indexa_por_seq_e_por_id() -> None:
    manifest = mf.Manifest(
        seed=1, folder="f", items=(_item(), _item(seq="000002", drive_item_id="B"))
    )

    assert set(manifest.by_seq()) == {"000001", "000002"}
    assert manifest.by_drive_id()["B"].seq == "000002"


def test_meta_features_codifica_sinais_e_escala() -> None:
    features = mf.meta_features(
        _item(
            mime_type="image/PNG",
            width=1080,
            height=2340,
            has_camera=False,
            has_taken_at=False,
            name_flags=("screenshot", "whatsapp"),
        )
    )

    named = dict(zip(mf.META_FEATURE_NAMES, features, strict=True))
    assert named["name_screenshot"] == 1.0
    assert named["name_whatsapp"] == 1.0
    assert named["name_camera"] == 0.0
    assert named["is_png"] == 1.0
    assert named["matches_phone_screen_ratio"] == 1.0
    assert named["has_camera"] == 0.0
    assert named["log_width"] == pytest.approx(math.log1p(1080) / 10)
    assert named["aspect_ratio"] == pytest.approx(1080 / 2340 / 4)


def test_meta_features_com_camera_heic_e_proporcao_extrema() -> None:
    features = mf.meta_features(
        _item(mime_type="image/heic", width=4000, height=100, has_camera=True, has_taken_at=True)
    )

    named = dict(zip(mf.META_FEATURE_NAMES, features, strict=True))
    assert (named["has_camera"], named["has_taken_at"], named["is_heic"]) == (1.0, 1.0, 1.0)
    assert named["aspect_ratio"] == 1.0  # limitada em 4:1
    assert named["matches_phone_screen_ratio"] == 0.0


def test_meta_features_sem_dimensoes_nem_mime_viram_zero() -> None:
    features = mf.meta_features(_item())

    assert features == (0.0,) * len(mf.META_FEATURE_NAMES)
