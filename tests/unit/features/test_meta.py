# SPDX-License-Identifier: GPL-3.0-only
"""Testes do extrator de features de metadados (TF-151-01, plan 3.1, meta_version=1).

Dados ausentes ou malformados viram zero e nunca são evidência (spec, caso de borda).
"""

import math
from dataclasses import replace

import numpy as np
import numpy.typing as npt
import pytest

from odc.features.meta import FEATURE_NAMES, META_VERSION, NAME_DERIVED_FEATURES, extract
from odc.storage.dto import ItemRow

BASE = ItemRow(
    id=1,
    drive_item_id="FAKE-ITEM-0001",
    scan_root_id=1,
    parent_id="FAKE-PARENT-0001",
    name="foto-sintetica-001.jpg",
    mime_type="image/jpeg",
    size_bytes=2_000_000,
    width=4032,
    height=3024,
    camera_make=None,
    camera_model=None,
    taken_at=None,
    has_location=False,
    remote_created_at="2024-05-01T10:00:00+00:00",
    remote_modified_at="2024-05-02T11:30:00+00:00",
    ctag="FAKE-CTAG-0001",
    etag="FAKE-ETAG-0001",
    state="DESCOBERTO",
    failure_reason=None,
    failure_count=0,
    created_at="2024-05-01T10:00:00+00:00",
    updated_at="2024-05-02T11:30:00+00:00",
)

PHONE_PNG = replace(
    BASE,
    name="Screenshot_2024-01-01-10-00-00.png",
    mime_type="image/png",
    size_bytes=900_000,
    width=1170,
    height=2532,
)


def _value(vector: npt.NDArray[np.float32], name: str) -> float:
    return float(vector[FEATURE_NAMES.index(name)])


class TestContract:
    def test_meta_version_is_one(self) -> None:
        assert META_VERSION == 1

    def test_feature_names_are_fixed_and_ordered(self) -> None:
        assert FEATURE_NAMES == (
            "is_png",
            "is_heic",
            "has_camera",
            "has_taken_at",
            "has_location",
            "log_width",
            "log_height",
            "aspect_ratio",
            "matches_phone_screen_ratio",
            "name_screenshot",
            "name_whatsapp",
            "name_camera",
            "log_size_bytes",
            "bytes_per_pixel",
        )

    def test_name_derived_features_are_the_name_and_extension_flags(self) -> None:
        assert set(NAME_DERIVED_FEATURES) <= set(FEATURE_NAMES)
        assert NAME_DERIVED_FEATURES == (
            "is_png",
            "is_heic",
            "name_screenshot",
            "name_whatsapp",
            "name_camera",
        )

    def test_renaming_only_changes_name_derived_features(self) -> None:
        renamed = replace(BASE, name="Screenshot_20240101-120000.png", mime_type="image/png")

        changed = {
            name
            for name, before, after in zip(
                FEATURE_NAMES, extract(BASE), extract(renamed), strict=True
            )
            if before != after
        }

        assert changed
        assert changed <= set(NAME_DERIVED_FEATURES)

    def test_vector_is_float32_with_one_position_per_feature(self) -> None:
        vector = extract(BASE)

        assert vector.dtype == np.float32
        assert vector.shape == (len(FEATURE_NAMES),) == (14,)

    def test_vector_is_stable_across_calls_and_does_not_mutate_input(self) -> None:
        first = extract(PHONE_PNG)
        second = extract(PHONE_PNG)

        assert np.array_equal(first, second)
        assert PHONE_PNG.mime_type == "image/png"


class TestAcceptanceCriteria:
    def test_phone_screenshot_png_without_camera(self) -> None:
        vector = extract(PHONE_PNG)

        assert _value(vector, "matches_phone_screen_ratio") == 1.0
        assert _value(vector, "is_png") == 1.0
        assert _value(vector, "has_camera") == 0.0

    def test_whatsapp_name_sets_flag(self) -> None:
        item = replace(BASE, name="IMG-20240101-WA0001.jpg")

        assert _value(extract(item), "name_whatsapp") == 1.0


class TestFormatFlags:
    @pytest.mark.parametrize(
        ("mime_type", "is_png", "is_heic"),
        [
            ("image/png", 1.0, 0.0),
            ("IMAGE/PNG", 1.0, 0.0),
            (" image/png ", 1.0, 0.0),
            ("image/heic", 0.0, 1.0),
            ("image/heif", 0.0, 1.0),
            ("image/jpeg", 0.0, 0.0),
            ("", 0.0, 0.0),
            ("video/mp4", 0.0, 0.0),
        ],
    )
    def test_mime_type_flags(self, mime_type: str, is_png: float, is_heic: float) -> None:
        vector = extract(replace(BASE, mime_type=mime_type))

        assert _value(vector, "is_png") == is_png
        assert _value(vector, "is_heic") == is_heic


class TestCameraFlags:
    @pytest.mark.parametrize(
        ("camera_make", "camera_model", "expected"),
        [
            ("Apple", None, 1.0),
            (None, "iPhone 15", 1.0),
            ("Apple", "iPhone 15", 1.0),
            (None, None, 0.0),
            ("", None, 0.0),
            ("   ", "\t", 0.0),
        ],
    )
    def test_has_camera_requires_non_blank_make_or_model(
        self, camera_make: str | None, camera_model: str | None, expected: float
    ) -> None:
        item = replace(BASE, camera_make=camera_make, camera_model=camera_model)

        assert _value(extract(item), "has_camera") == expected


class TestTakenAt:
    @pytest.mark.parametrize(
        ("taken_at", "expected"),
        [
            ("2024-04-30T18:15:00+00:00", 1.0),
            ("2024-04-30T18:15:00", 1.0),
            (None, 0.0),
            ("", 0.0),
            ("não é data", 0.0),
            ("2024-13-45T99:00:00", 0.0),
        ],
    )
    def test_has_taken_at_ignores_malformed_dates(
        self, taken_at: str | None, expected: float
    ) -> None:
        item = replace(BASE, taken_at=taken_at)

        assert _value(extract(item), "has_taken_at") == expected


class TestLocationFlag:
    @pytest.mark.parametrize(("has_location", "expected"), [(True, 1.0), (False, 0.0)])
    def test_has_location_mirrors_item(self, has_location: bool, expected: float) -> None:
        item = replace(BASE, has_location=has_location)

        assert _value(extract(item), "has_location") == expected


class TestDimensions:
    def test_logs_and_aspect_ratio_for_valid_dimensions(self) -> None:
        item = replace(BASE, width=1000, height=500)

        vector = extract(item)

        assert _value(vector, "log_width") == pytest.approx(math.log1p(1000))
        assert _value(vector, "log_height") == pytest.approx(math.log1p(500))
        assert _value(vector, "aspect_ratio") == pytest.approx(2.0)

    @pytest.mark.parametrize(
        ("width", "height"),
        [(None, None), (None, 2532), (1170, None), (0, 2532), (1170, 0), (-5, 2532), (1170, -1)],
    )
    def test_missing_or_non_positive_dimensions_become_zero(
        self, width: int | None, height: int | None
    ) -> None:
        item = replace(BASE, width=width, height=height)

        vector = extract(item)

        assert _value(vector, "aspect_ratio") == 0.0
        assert _value(vector, "matches_phone_screen_ratio") == 0.0
        assert _value(vector, "bytes_per_pixel") == 0.0
        assert not np.isnan(vector).any()

    def test_missing_width_still_logs_height(self) -> None:
        vector = extract(replace(BASE, width=None, height=500))

        assert _value(vector, "log_width") == 0.0
        assert _value(vector, "log_height") == pytest.approx(math.log1p(500))

    @pytest.mark.parametrize(
        ("width", "height", "expected"),
        [
            (1170, 2532, 1.0),
            (2532, 1170, 1.0),
            (1080, 1920, 1.0),
            (1080, 2400, 1.0),
            (3024, 4032, 1.0),  # 4:3 está na tabela (tablet); coincide com a câmera
            (1000, 1000, 0.0),
            (1170, 2000, 0.0),
        ],
    )
    def test_matches_phone_screen_ratio(self, width: int, height: int, expected: float) -> None:
        item = replace(BASE, width=width, height=height)

        assert _value(extract(item), "matches_phone_screen_ratio") == expected


class TestNameFlags:
    @pytest.mark.parametrize(
        ("name", "screenshot", "whatsapp", "camera"),
        [
            ("Screenshot_2024.png", 1.0, 0.0, 0.0),
            ("Captura de Tela 2024.png", 1.0, 0.0, 0.0),
            ("IMG-20240101-WA0001.jpg", 0.0, 1.0, 0.0),
            ("IMG_1234.jpg", 0.0, 0.0, 1.0),
            ("PXL_20240101.jpg", 0.0, 0.0, 1.0),
            ("DSC00042.jpg", 0.0, 0.0, 1.0),
            ("foto-ferias.jpg", 0.0, 0.0, 0.0),
            ("Férias Captura de Tela.png", 1.0, 0.0, 0.0),
        ],
    )
    def test_name_flags(self, name: str, screenshot: float, whatsapp: float, camera: float) -> None:
        vector = extract(replace(BASE, name=name))

        assert _value(vector, "name_screenshot") == screenshot
        assert _value(vector, "name_whatsapp") == whatsapp
        assert _value(vector, "name_camera") == camera


class TestSize:
    @pytest.mark.parametrize("size_bytes", [1, 1_024, 2_000_000, 50_000_000])
    def test_log_size_bytes(self, size_bytes: int) -> None:
        vector = extract(replace(BASE, size_bytes=size_bytes))

        assert _value(vector, "log_size_bytes") == pytest.approx(math.log1p(size_bytes))

    def test_bytes_per_pixel_uses_valid_dimensions(self) -> None:
        item = replace(BASE, size_bytes=1_200_000, width=1000, height=1000)

        assert _value(extract(item), "bytes_per_pixel") == pytest.approx(1.2)

    @pytest.mark.parametrize("size_bytes", [0, -10])
    def test_non_positive_size_becomes_zero(self, size_bytes: int) -> None:
        vector = extract(replace(BASE, size_bytes=size_bytes))

        assert _value(vector, "log_size_bytes") == 0.0
        assert _value(vector, "bytes_per_pixel") == 0.0

    def test_unicode_name_with_valid_metadata_extracts_without_error(self) -> None:
        item = replace(BASE, name="Árvore_ÇÃO-WA1234 🌳.jpg")

        vector = extract(item)

        assert _value(vector, "name_whatsapp") == 1.0
        assert vector.shape == (14,)
