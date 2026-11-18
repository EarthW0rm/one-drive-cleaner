# SPDX-License-Identifier: GPL-3.0-only
"""Testes dos modelos de resposta da Graph: campos malformados viram ausentes (plan 4.2)."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from odc.graph.models import (
    BatchItem,
    BatchResponse,
    DeltaPage,
    Drive,
    DriveItem,
    GraphErrorBody,
    ParentReference,
    ThumbnailUrl,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "graph"


def _load(name: str) -> object:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return cast(object, json.load(handle))


def _as_dict(name: str) -> dict[str, object]:
    payload = _load(name)
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


class TestDriveItemValid:
    def test_parses_photo_item_fields(self) -> None:
        item = DriveItem.model_validate(_load("drive_item_photo.json"))

        assert item.id == "FAKE-ITEM-0001"
        assert item.name == "foto-sintetica-001.jpg"
        assert item.size == 2048576
        assert item.c_tag == "FAKE-CTAG-0001"
        assert item.e_tag == "FAKE-ETAG-0001"
        assert item.created_datetime == datetime(2024, 5, 1, 10, 0, tzinfo=UTC)
        assert item.last_modified_datetime == datetime(2024, 5, 2, 11, 30, tzinfo=UTC)

    def test_parses_nested_facets(self) -> None:
        item = DriveItem.model_validate(_load("drive_item_photo.json"))

        assert item.parent_reference == ParentReference(
            id="FAKE-PARENT-0001",
            drive_id="FAKE-DRIVE-0001",
            path="/drive/root:/Pasta Sintetica",
        )
        assert item.file is not None
        assert item.file.mime_type == "image/jpeg"
        assert item.image is not None
        assert (item.image.width, item.image.height) == (4032, 3024)
        assert item.photo is not None
        assert item.photo.camera_make == "FakeMake"
        assert item.photo.camera_model == "FakeModel 1"
        assert item.photo.taken_datetime == datetime(2024, 4, 30, 18, 15, tzinfo=UTC)

    def test_valid_item_has_no_flags_or_folder(self) -> None:
        item = DriveItem.model_validate(_load("drive_item_photo.json"))

        assert item.deleted is False
        assert item.location_present is False
        assert item.folder is None

    def test_ignores_unknown_keys(self) -> None:
        payload = _as_dict("drive_item_photo.json")
        payload["@microsoft.graph.downloadUrl"] = "https://example.invalid/FAKE-SIGNATURE"
        payload["campoDesconhecido"] = {"x": 1}

        item = DriveItem.model_validate(payload)

        assert item.id == "FAKE-ITEM-0001"
        assert "FAKE-SIGNATURE" not in repr(item)


class TestDriveItemMalformed:
    def test_wrong_typed_fields_become_none(self) -> None:
        item = DriveItem.model_validate(_load("drive_item_malformed.json"))

        assert item.id == "FAKE-ITEM-0002"
        assert item.name is None
        assert item.size is None
        assert item.c_tag is None
        assert item.created_datetime is None
        assert item.parent_reference is None

    def test_image_string_width_becomes_none_without_exception(self) -> None:
        item = DriveItem.model_validate(_load("drive_item_malformed.json"))

        assert item.image is not None
        assert item.image.width is None
        assert item.image.height is None

    def test_file_facet_with_wrong_mime_type_becomes_none(self) -> None:
        item = DriveItem.model_validate(_load("drive_item_malformed.json"))

        assert item.file is not None
        assert item.file.mime_type is None

    def test_photo_facet_with_invalid_values_becomes_none(self) -> None:
        item = DriveItem.model_validate(_load("drive_item_malformed.json"))

        assert item.photo is not None
        assert item.photo.camera_make is None
        assert item.photo.camera_model is None
        assert item.photo.taken_datetime is None

    def test_location_non_object_is_not_present(self) -> None:
        item = DriveItem.model_validate(_load("drive_item_malformed.json"))

        assert item.location_present is False

    @pytest.mark.parametrize(
        ("width", "expected_width"),
        [(0, None), (-1, None), (True, None), (1.5, None), ("1024", None), (1, 1)],
    )
    def test_invalid_width_becomes_none(self, width: object, expected_width: int | None) -> None:
        payload = _as_dict("drive_item_photo.json")
        payload["image"] = {"width": width, "height": 10}

        item = DriveItem.model_validate(payload)

        assert item.image is not None
        assert item.image.width == expected_width
        assert item.image.height == 10

    @pytest.mark.parametrize("height", [0, -5, False, 2.0, "768"])
    def test_invalid_height_becomes_none(self, height: object) -> None:
        payload = _as_dict("drive_item_photo.json")
        payload["image"] = {"width": 10, "height": height}

        item = DriveItem.model_validate(payload)

        assert item.image is not None
        assert item.image.height is None
        assert item.image.width == 10

    def test_non_object_facet_becomes_none(self) -> None:
        payload = _as_dict("drive_item_photo.json")
        payload["image"] = "abc"

        item = DriveItem.model_validate(payload)

        assert item.image is None

    def test_taken_datetime_non_string_becomes_none(self) -> None:
        payload = _as_dict("drive_item_photo.json")
        payload["photo"] = {"takenDateTime": 1714500000}

        item = DriveItem.model_validate(payload)

        assert item.photo is not None
        assert item.photo.taken_datetime is None

    def test_taken_datetime_accepts_datetime_instance(self) -> None:
        moment = datetime(2024, 4, 30, 18, 15, tzinfo=UTC)
        payload = _as_dict("drive_item_photo.json")
        payload["photo"] = {"takenDateTime": moment}

        item = DriveItem.model_validate(payload)

        assert item.photo is not None
        assert item.photo.taken_datetime == moment

    def test_size_zero_is_kept(self) -> None:
        payload = _as_dict("drive_item_photo.json")
        payload["size"] = 0

        item = DriveItem.model_validate(payload)

        assert item.size == 0

    def test_parent_reference_without_id_becomes_none(self) -> None:
        payload = _as_dict("drive_item_photo.json")
        payload["parentReference"] = {"driveId": "FAKE-DRIVE-0001"}

        item = DriveItem.model_validate(payload)

        assert item.parent_reference is None

    def test_parent_reference_blank_id_becomes_none(self) -> None:
        payload = _as_dict("drive_item_photo.json")
        payload["parentReference"] = {"id": ""}

        item = DriveItem.model_validate(payload)

        assert item.parent_reference is None

    def test_non_string_name_becomes_none_and_keeps_item(self) -> None:
        payload = _as_dict("drive_item_photo.json")
        payload["name"] = ["lista"]

        item = DriveItem.model_validate(payload)

        assert item.id == "FAKE-ITEM-0001"
        assert item.name is None


class TestDriveItemRequiredId:
    @pytest.mark.parametrize("payload", ["texto", 42, ["lista"], None])
    def test_non_object_payload_raises_validation_error(self, payload: object) -> None:
        with pytest.raises(ValidationError):
            DriveItem.model_validate(payload)

    def test_missing_id_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            DriveItem.model_validate(_load("drive_item_missing_id.json"))

    @pytest.mark.parametrize("bad_id", ["", 42, None])
    def test_invalid_id_raises_validation_error(self, bad_id: object) -> None:
        payload = _as_dict("drive_item_photo.json")
        payload["id"] = bad_id

        with pytest.raises(ValidationError):
            DriveItem.model_validate(payload)


class TestDriveItemFlags:
    def test_deleted_facet_is_recognized(self) -> None:
        item = DriveItem.model_validate(_load("drive_item_deleted.json"))

        assert item.deleted is True
        assert item.name is None

    def test_deleted_true_flag_is_recognized(self) -> None:
        payload = _as_dict("drive_item_photo.json")
        payload["deleted"] = True

        item = DriveItem.model_validate(payload)

        assert item.deleted is True

    def test_deleted_non_facet_value_is_not_deleted(self) -> None:
        payload = _as_dict("drive_item_photo.json")
        payload["deleted"] = "sim"

        item = DriveItem.model_validate(payload)

        assert item.deleted is False

    def test_folder_item_has_folder_facet_and_no_file(self) -> None:
        item = DriveItem.model_validate(_load("drive_item_folder.json"))

        assert item.folder is not None
        assert item.folder.child_count == 3
        assert item.file is None
        assert item.image is None

    def test_folder_child_count_invalid_becomes_none(self) -> None:
        payload = _as_dict("drive_item_folder.json")
        payload["folder"] = {"childCount": "muitos"}

        item = DriveItem.model_validate(payload)

        assert item.folder is not None
        assert item.folder.child_count is None

    def test_non_image_keeps_mime_and_marks_location(self) -> None:
        item = DriveItem.model_validate(_load("drive_item_non_image.json"))

        assert item.file is not None
        assert item.file.mime_type == "application/pdf"
        assert item.location_present is True
        assert item.image is None

    def test_location_true_flag_is_not_location_object(self) -> None:
        payload = _as_dict("drive_item_photo.json")
        payload["location"] = True

        item = DriveItem.model_validate(payload)

        assert item.location_present is False


class TestDeltaPage:
    def test_reads_next_link_and_keeps_only_valid_items(self) -> None:
        page = DeltaPage.model_validate(_load("delta_page_next.json"))

        assert page.next_link is not None
        assert page.next_link.endswith("$skiptoken=FAKE-SKIP-0001")
        assert page.delta_link is None
        assert [item.id for item in page.items] == ["FAKE-ITEM-0001", "FAKE-ITEM-0002"]
        assert page.dropped == 1

    def test_reads_delta_link_and_deleted_items(self) -> None:
        page = DeltaPage.model_validate(_load("delta_page_final.json"))

        assert page.delta_link is not None
        assert page.delta_link.endswith("token=FAKE-DELTA-0001")
        assert page.next_link is None
        assert len(page.items) == 1
        assert page.items[0].deleted is True
        assert page.dropped == 0

    def test_value_not_list_yields_empty_items(self) -> None:
        page = DeltaPage.model_validate({"value": "nao-e-lista"})

        assert page.items == ()
        assert page.dropped == 0

    def test_missing_value_yields_empty_items(self) -> None:
        page = DeltaPage.model_validate({"@odata.deltaLink": "https://example.invalid/d"})

        assert page.items == ()
        assert page.delta_link == "https://example.invalid/d"

    def test_non_object_payload_raises(self) -> None:
        with pytest.raises(ValidationError):
            DeltaPage.model_validate("texto")


class TestBatchResponse:
    def test_parses_entries_and_bodies(self) -> None:
        batch = BatchResponse.model_validate(_load("batch_response.json"))

        assert [entry.id for entry in batch.responses] == ["1", "2"]
        assert batch.dropped == 1
        ok, not_found = batch.responses
        assert ok.status == 200
        assert ok.body == {
            "url": "https://example.invalid/thumb/FAKE-THUMB-0001?sig=FAKE-SIGNATURE"
        }
        assert not_found.status == 404
        assert not_found.body == {
            "error": {"code": "itemNotFound", "message": "Item sintetico ausente"}
        }

    def test_non_object_body_becomes_none(self) -> None:
        entry = BatchItem.model_validate({"id": "9", "status": 200, "body": "texto"})

        assert entry.body is None

    def test_status_must_be_integer(self) -> None:
        with pytest.raises(ValidationError):
            BatchItem.model_validate({"id": "9", "status": "200"})

    @pytest.mark.parametrize("payload", ["texto", 42, None])
    def test_non_object_payload_raises(self, payload: object) -> None:
        with pytest.raises(ValidationError):
            BatchResponse.model_validate(payload)

    def test_responses_not_list_yields_empty(self) -> None:
        batch = BatchResponse.model_validate({"responses": {"id": "1"}})

        assert batch.responses == ()
        assert batch.dropped == 0


class TestThumbnailUrl:
    def test_parses_url(self) -> None:
        thumb = ThumbnailUrl.model_validate(_load("thumbnail.json"))

        assert thumb.url.startswith("https://example.invalid/thumb/FAKE-THUMB-0003")

    def test_url_is_hidden_from_repr(self) -> None:
        thumb = ThumbnailUrl.model_validate(_load("thumbnail.json"))

        assert "FAKE-SIGNATURE" not in repr(thumb)

    @pytest.mark.parametrize("bad", ["http://example.invalid/x", "", "ftp://example.invalid"])
    def test_non_https_or_empty_url_raises(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            ThumbnailUrl.model_validate({"url": bad})


class TestDrive:
    def test_parses_drive(self) -> None:
        drive = Drive.model_validate(_load("drive.json"))

        assert drive.id == "FAKE-DRIVE-0001"
        assert drive.drive_type == "personal"

    def test_non_string_drive_type_becomes_none(self) -> None:
        drive = Drive.model_validate({"id": "FAKE-DRIVE-0002", "driveType": 1})

        assert drive.drive_type is None


class TestGraphErrorBody:
    def test_parses_error_envelope(self) -> None:
        body = GraphErrorBody.from_payload(_load("error_item_not_found.json"))

        assert body.code == "itemNotFound"
        assert body.message == "Item sintetico ausente"

    @pytest.mark.parametrize("payload", [None, "texto fora de JSON", 42, [1, 2]])
    def test_non_envelope_payload_yields_unknown_code(self, payload: object) -> None:
        body = GraphErrorBody.from_payload(payload)

        assert body.code == "unknown"
        assert body.message == ""

    @pytest.mark.parametrize(
        "error",
        ["texto", {"code": "", "message": 1}, {"message": "sem codigo"}],
    )
    def test_malformed_error_fields_fall_back(self, error: object) -> None:
        body = GraphErrorBody.from_payload({"error": error})

        assert body.code == "unknown"
        assert body.message in {"", "sem codigo"}
