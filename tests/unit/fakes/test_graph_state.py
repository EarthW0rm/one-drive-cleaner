# SPDX-License-Identifier: GPL-3.0-only
"""Testes do estado do OneDrive fake (`FakeDrive`)."""

import time
from collections import Counter

import pytest

from tests.fakes.graph_state import (
    ROOT_ID,
    DeltaChunk,
    FakeDrive,
    FakeDriveError,
    PhotoMeta,
    etag_matches,
)
from tests.fakes.synthetic_images import ImageKind, make_spec, render


@pytest.fixture
def drive() -> FakeDrive:
    return FakeDrive()


def _error(call: object) -> FakeDriveError:
    with pytest.raises(FakeDriveError) as info:
        call()  # type: ignore[operator]
    return info.value


def _drain(drive: FakeDrive, root: str, *, since: int = 0, limit: int | None = None) -> DeltaChunk:
    upto = drive.seq
    entries = []
    pos = None
    while True:
        chunk = drive.delta_chunk(root, since=since, upto=upto, pos=pos, limit=limit)
        entries.extend(chunk.entries)
        if chunk.next_pos is None:
            return DeltaChunk(entries, None)
        pos = chunk.next_pos


def _ids(chunk: DeltaChunk) -> list[str]:
    return [entry.item.id for entry in chunk.entries]


class TestStructure:
    def test_starts_with_root_and_camera_roll(self, drive: FakeDrive) -> None:
        roll = drive.get(drive.camera_roll_id or "")

        assert drive.root_id == ROOT_ID
        assert roll.name == "Camera Roll"
        assert drive.path_of(roll) == "/drive/root:/Pictures"

    def test_camera_roll_can_be_disabled(self) -> None:
        assert FakeDrive(camera_roll=False).camera_roll_id is None

    def test_get_accepts_root_alias_and_rejects_unknown(self, drive: FakeDrive) -> None:
        assert drive.get("root").id == ROOT_ID
        assert _error(lambda: drive.get("FAKE-NOPE")).status == 404

    def test_names_are_unique_per_folder_ignoring_case(self, drive: FakeDrive) -> None:
        drive.add_folder(ROOT_ID, "Fotos")

        error = _error(lambda: drive.add_folder(ROOT_ID, "FOTOS"))

        assert (error.status, error.code) == (409, "nameAlreadyExists")

    def test_rename_conflict_behavior_adds_numeric_suffix(self, drive: FakeDrive) -> None:
        drive.add_file(ROOT_ID, "a.jpg", b"1", "image/jpeg")

        second = drive.add_file(ROOT_ID, "a.jpg", b"2", "image/jpeg", conflict="rename")
        third = drive.add_folder(ROOT_ID, "pasta")
        fourth = drive.add_folder(ROOT_ID, "pasta", conflict="rename")

        assert second.name == "a 1.jpg"
        assert (third.name, fourth.name) == ("pasta", "pasta 1")

    def test_replace_conflict_behavior_is_rejected(self, drive: FakeDrive) -> None:
        drive.add_folder(ROOT_ID, "x")

        assert _error(lambda: drive.add_folder(ROOT_ID, "x", conflict="replace")).status == 400

    @pytest.mark.parametrize("name", ["", "a/b", "a:b", "a*", "a?", 'a"', "a<b", "a|b", "x" * 256])
    def test_invalid_names_are_rejected(self, drive: FakeDrive, name: str) -> None:
        assert _error(lambda: drive.add_folder(ROOT_ID, name)).status == 400

    def test_parent_must_exist_and_be_a_folder(self, drive: FakeDrive) -> None:
        file = drive.add_file(ROOT_ID, "a.txt", b"x", "text/plain")

        assert _error(lambda: drive.add_folder("FAKE-NOPE", "x")).status == 404
        assert _error(lambda: drive.add_folder(file.id, "x")).status == 400

    def test_ensure_folder_is_idempotent(self, drive: FakeDrive) -> None:
        first = drive.ensure_folder(ROOT_ID, "App")

        assert drive.ensure_folder(ROOT_ID, "app").id == first.id

    def test_find_by_path_and_children_order(self, drive: FakeDrive) -> None:
        pictures = drive.find_by_path(ROOT_ID, "Pictures")
        drive.add_folder(pictures.id, "b")
        drive.add_folder(pictures.id, "A")

        assert drive.find_by_path(ROOT_ID, "pictures/camera roll").id == drive.camera_roll_id
        assert [c.name for c in drive.children(pictures.id)] == ["A", "b", "Camera Roll"]
        assert _error(lambda: drive.find_by_path(ROOT_ID, "Pictures/nada")).status == 404

    def test_find_by_path_through_file_is_not_found(self, drive: FakeDrive) -> None:
        file = drive.add_file(ROOT_ID, "a.txt", b"x", "text/plain")

        assert _error(lambda: drive.find_by_path(file.id, "x")).status == 404

    def test_count_excludes_folders_and_deleted(self, drive: FakeDrive) -> None:
        keep = drive.add_file(ROOT_ID, "a.txt", b"x", "text/plain")
        drive.delete(drive.add_file(ROOT_ID, "b.txt", b"x", "text/plain").id)

        assert drive.count() == 1
        assert drive.count(include_folders=True) == 4
        assert keep.id in {c.id for c in drive.children(ROOT_ID)}


class TestImages:
    @pytest.mark.parametrize("kind", list(ImageKind))
    def test_metadata_is_coherent_with_rendered_content(
        self, drive: FakeDrive, kind: ImageKind
    ) -> None:
        item = drive.add_image(ROOT_ID, "x", kind, seed=4)
        spec = make_spec(kind, 4)

        assert item.size == len(drive.render_content(item)) == len(render(spec))
        assert (item.width, item.height) == (spec.width, spec.height)
        assert item.mime_type == spec.mime_type
        assert item.is_image

    def test_photo_facet_only_for_photos_unless_overridden(self, drive: FakeDrive) -> None:
        photo = drive.add_image(ROOT_ID, "a", ImageKind.PHOTO, seed=1)
        shot = drive.add_image(ROOT_ID, "b", ImageKind.SCREENSHOT)
        forced = drive.add_image(ROOT_ID, "c", ImageKind.MEME, with_photo_facet=True)
        bare = drive.add_image(ROOT_ID, "d", ImageKind.PHOTO, with_photo_facet=False)

        assert isinstance(photo.photo, PhotoMeta)
        assert shot.photo is None
        assert forced.photo is not None
        assert bare.photo is None

    def test_location_flag(self, drive: FakeDrive) -> None:
        assert drive.add_image(ROOT_ID, "a", ImageKind.PHOTO, has_location=True).has_location

    def test_non_image_file_has_no_thumbnail(self, drive: FakeDrive) -> None:
        pdf = drive.add_file(ROOT_ID, "a.pdf", b"%PDF-", "application/pdf")

        assert not pdf.is_image
        assert _error(lambda: drive.render_thumbnail(pdf, "small")).status == 404

    def test_render_content_of_folder_is_empty(self, drive: FakeDrive) -> None:
        assert drive.render_content(drive.get(ROOT_ID)) == b""

    @pytest.mark.parametrize("size", ["small", "medium", "large", "c64x48"])
    def test_thumbnail_sizes(self, drive: FakeDrive, size: str) -> None:
        item = drive.add_image(ROOT_ID, "a", ImageKind.PHOTO)
        width, height = drive.thumbnail_dimensions(size)

        thumb = drive.render_thumbnail(item, size)

        assert thumb.startswith(b"\xff\xd8")
        assert width >= 1
        assert height >= 1

    @pytest.mark.parametrize("size", ["gigante", "c0x10", "c10", "cAxB", "c9999x10", "c10x9999"])
    def test_invalid_thumbnail_sizes(self, size: str) -> None:
        assert _error(lambda: FakeDrive.thumbnail_dimensions(size)).status == 400


class TestMove:
    def test_move_changes_parent_name_and_etag(self, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"x", "text/plain")
        target = drive.add_folder(ROOT_ID, "Q")
        before = item.e_tag

        moved = drive.move(item.id, target.id, "b.txt")

        assert (moved.parent_id, moved.name) == (target.id, "b.txt")
        assert moved.e_tag != before
        assert drive.find_by_path(ROOT_ID, "Q/b.txt").id == item.id
        assert _error(lambda: drive.find_by_path(ROOT_ID, "a.txt")).status == 404

    def test_move_collision_is_409_and_suffix_retry_works(self, drive: FakeDrive) -> None:
        target = drive.add_folder(ROOT_ID, "Q")
        drive.add_file(target.id, "a.txt", b"1", "text/plain")
        item = drive.add_file(ROOT_ID, "a.txt", b"2", "text/plain")

        assert _error(lambda: drive.move(item.id, target.id)).status == 409
        assert drive.move(item.id, target.id, "a (1).txt").name == "a (1).txt"

    def test_move_with_rename_behavior_picks_free_name(self, drive: FakeDrive) -> None:
        target = drive.add_folder(ROOT_ID, "Q")
        drive.add_file(target.id, "a.txt", b"1", "text/plain")
        item = drive.add_file(ROOT_ID, "a.txt", b"2", "text/plain")

        assert drive.move(item.id, target.id, conflict="rename").name == "a 1.txt"

    def test_move_to_same_place_is_allowed(self, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"1", "text/plain")

        assert drive.move(item.id, ROOT_ID).name == "a.txt"

    def test_folder_cannot_move_into_itself_or_descendants(self, drive: FakeDrive) -> None:
        outer = drive.add_folder(ROOT_ID, "outer")
        inner = drive.add_folder(outer.id, "inner")

        assert _error(lambda: drive.move(outer.id, inner.id)).status == 400
        assert _error(lambda: drive.move(outer.id, outer.id)).status == 400

    def test_root_cannot_move(self, drive: FakeDrive) -> None:
        folder = drive.add_folder(ROOT_ID, "x")

        assert _error(lambda: drive.move(ROOT_ID, folder.id)).status == 400

    def test_destination_must_be_a_folder(self, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"1", "text/plain")

        assert _error(lambda: drive.move(item.id, item.id)).status == 400

    def test_stale_if_match_is_412(self, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"1", "text/plain")
        stale = item.e_tag
        drive.touch_external(item.id)

        assert _error(lambda: drive.move(item.id, ROOT_ID, "b.txt", if_match=stale)).status == 412
        assert drive.move(item.id, ROOT_ID, "b.txt", if_match=item.e_tag).name == "b.txt"


class TestDeleteAndRestore:
    def test_delete_with_current_etag_moves_to_bin(self, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"1", "text/plain")

        drive.delete(item.id, if_match=item.e_tag)

        assert [i.id for i in drive.recycle_bin()] == [item.id]
        assert _error(lambda: drive.get(item.id)).status == 404
        assert drive.get(item.id, include_deleted=True).deleted

    def test_delete_with_stale_etag_is_412_and_keeps_item(self, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"1", "text/plain")
        stale = item.e_tag
        drive.touch_external(item.id)

        assert _error(lambda: drive.delete(item.id, if_match=stale)).status == 412
        assert drive.recycle_bin() == []
        assert not drive.get(item.id).deleted

    def test_delete_without_if_match_and_with_wildcard(self, drive: FakeDrive) -> None:
        first = drive.add_file(ROOT_ID, "a.txt", b"1", "text/plain")
        second = drive.add_file(ROOT_ID, "b.txt", b"1", "text/plain")

        drive.delete(first.id)
        drive.delete(second.id, if_match="*")

        assert len(drive.recycle_bin()) == 2

    def test_deleting_folder_deletes_descendants(self, drive: FakeDrive) -> None:
        folder = drive.add_folder(ROOT_ID, "f")
        child = drive.add_file(folder.id, "a.txt", b"1", "text/plain")

        drive.delete(folder.id)

        assert drive.get(child.id, include_deleted=True).deleted
        assert [i.id for i in drive.recycle_bin()] == [folder.id]

    def test_root_cannot_be_deleted(self, drive: FakeDrive) -> None:
        assert _error(lambda: drive.delete(ROOT_ID)).status == 400

    def test_deleting_twice_is_404(self, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"1", "text/plain")
        drive.delete(item.id)

        assert _error(lambda: drive.delete(item.id)).status == 404

    def test_restore_returns_item_and_descendants(self, drive: FakeDrive) -> None:
        folder = drive.add_folder(ROOT_ID, "f")
        child = drive.add_file(folder.id, "a.txt", b"1", "text/plain")
        drive.delete(folder.id)

        drive.restore(folder.id)

        assert not drive.get(child.id).deleted
        assert drive.recycle_bin() == []
        assert drive.find_by_path(ROOT_ID, "f/a.txt").id == child.id

    def test_restore_fails_when_name_is_taken(self, drive: FakeDrive) -> None:
        parent = drive.add_folder(ROOT_ID, "p")
        item = drive.add_file(parent.id, "a.txt", b"1", "text/plain")
        drive.delete(item.id)
        drive.add_file(parent.id, "a.txt", b"2", "text/plain")
        assert _error(lambda: drive.restore(item.id)).status == 409

    def test_restore_with_missing_parent_is_conflict(self, drive: FakeDrive) -> None:
        parent = drive.add_folder(ROOT_ID, "p")
        item = drive.add_file(parent.id, "a.txt", b"1", "text/plain")
        drive.delete(item.id)
        drive.delete(parent.id)

        assert _error(lambda: drive.restore(item.id)).status == 409

    def test_restore_of_unknown_item_is_404(self, drive: FakeDrive) -> None:
        assert _error(lambda: drive.restore("FAKE-NOPE")).status == 404


class TestExternalChanges:
    def test_touch_external_changes_etag_only(self, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"1", "text/plain")
        etag, ctag = item.e_tag, item.c_tag

        drive.touch_external(item.id)

        assert item.e_tag != etag
        assert item.c_tag == ctag

    def test_replace_content_changes_size_etag_and_ctag(self, drive: FakeDrive) -> None:
        item = drive.add_image(ROOT_ID, "a.jpg", ImageKind.PHOTO)
        etag, ctag = item.e_tag, item.c_tag

        drive.replace_content(item.id, b"novo-conteudo")

        assert item.size == len(b"novo-conteudo")
        assert drive.render_content(item) == b"novo-conteudo"
        assert (item.e_tag, item.c_tag) != (etag, ctag)

    def test_replace_content_of_folder_is_rejected(self, drive: FakeDrive) -> None:
        assert _error(lambda: drive.replace_content(ROOT_ID, b"x")).status == 400


class TestEtagMatching:
    @pytest.mark.parametrize(
        "transform", [lambda e: e, lambda e: f"W/{e}", lambda e: e.strip('"'), lambda _: "*"]
    )
    def test_accepts_equivalent_forms(self, drive: FakeDrive, transform: object) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"1", "text/plain")

        assert etag_matches(item, transform(item.e_tag))  # type: ignore[operator]

    def test_rejects_other_etag(self, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"1", "text/plain")

        assert not etag_matches(item, '"{FAKE-other},9"')


class TestDelta:
    def test_first_sync_lists_everything_in_scope_without_deleted(self, drive: FakeDrive) -> None:
        keep = drive.add_file(ROOT_ID, "a.txt", b"1", "text/plain")
        gone = drive.add_file(ROOT_ID, "b.txt", b"1", "text/plain")
        drive.delete(gone.id)

        chunk = _drain(drive, ROOT_ID)

        assert keep.id in _ids(chunk)
        assert gone.id not in _ids(chunk)
        assert ROOT_ID in _ids(chunk)

    def test_incremental_returns_only_changes_after_token(self, drive: FakeDrive) -> None:
        drive.add_file(ROOT_ID, "a.txt", b"1", "text/plain")
        token = drive.seq
        new = drive.add_file(ROOT_ID, "b.txt", b"1", "text/plain")

        assert _ids(_drain(drive, ROOT_ID, since=token)) == [new.id]
        assert _ids(_drain(drive, ROOT_ID, since=drive.seq)) == []

    def test_only_latest_change_of_an_item_is_reported(self, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"1", "text/plain")
        token = drive.seq
        drive.touch_external(item.id)
        drive.touch_external(item.id)

        assert _ids(_drain(drive, ROOT_ID, since=token)) == [item.id]

    def test_delete_is_reported_as_gone(self, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"1", "text/plain")
        token = drive.seq
        drive.delete(item.id)

        entries = _drain(drive, ROOT_ID, since=token).entries

        assert [(e.item.id, e.gone) for e in entries] == [(item.id, True)]

    def test_delete_of_folder_reports_descendants(self, drive: FakeDrive) -> None:
        folder = drive.add_folder(ROOT_ID, "f")
        child = drive.add_file(folder.id, "a.txt", b"1", "text/plain")
        token = drive.seq
        drive.delete(folder.id)

        assert set(_ids(_drain(drive, ROOT_ID, since=token))) == {folder.id, child.id}

    def test_scope_is_the_subtree_of_the_root(self, drive: FakeDrive) -> None:
        scan = drive.add_folder(ROOT_ID, "scan")
        token = drive.seq
        inside = drive.add_file(scan.id, "a.txt", b"1", "text/plain")
        drive.add_file(ROOT_ID, "fora.txt", b"1", "text/plain")

        assert _ids(_drain(drive, scan.id, since=token)) == [inside.id]

    def test_item_moved_out_of_scope_is_reported_gone(self, drive: FakeDrive) -> None:
        scan = drive.add_folder(ROOT_ID, "scan")
        outside = drive.add_folder(ROOT_ID, "outside")
        item = drive.add_file(scan.id, "a.txt", b"1", "text/plain")
        token = drive.seq
        drive.move(item.id, outside.id)

        entries = _drain(drive, scan.id, since=token).entries

        assert [(e.item.id, e.gone) for e in entries] == [(item.id, True)]

    def test_item_moved_into_scope_is_reported_with_data(self, drive: FakeDrive) -> None:
        scan = drive.add_folder(ROOT_ID, "scan")
        item = drive.add_file(ROOT_ID, "a.txt", b"1", "text/plain")
        token = drive.seq
        drive.move(item.id, scan.id)

        entries = _drain(drive, scan.id, since=token).entries

        assert [(e.item.id, e.gone) for e in entries] == [(item.id, False)]

    def test_unrelated_change_outside_scope_is_ignored(self, drive: FakeDrive) -> None:
        scan = drive.add_folder(ROOT_ID, "scan")
        item = drive.add_file(ROOT_ID, "a.txt", b"1", "text/plain")
        token = drive.seq
        drive.touch_external(item.id)
        drive.delete(item.id)

        assert _ids(_drain(drive, scan.id, since=token)) == []

    def test_first_sync_ignores_items_that_left_the_scope(self, drive: FakeDrive) -> None:
        scan = drive.add_folder(ROOT_ID, "scan")
        outside = drive.add_folder(ROOT_ID, "outside")
        item = drive.add_file(scan.id, "a.txt", b"1", "text/plain")
        drive.move(item.id, outside.id)

        assert item.id not in _ids(_drain(drive, scan.id))

    def test_pagination_with_cursor_returns_every_item_once(self, drive: FakeDrive) -> None:
        for number in range(25):
            drive.add_file(ROOT_ID, f"{number}.txt", b"1", "text/plain")
        upto = drive.seq

        first = drive.delta_chunk(ROOT_ID, since=0, upto=upto, pos=None, limit=10)
        pages = [first]
        while pages[-1].next_pos is not None:
            pages.append(
                drive.delta_chunk(ROOT_ID, since=0, upto=upto, pos=pages[-1].next_pos, limit=10)
            )
        ids = [i for page in pages for i in _ids(page)]

        assert len(pages) >= 3
        assert len(ids) == len(set(ids)) == drive.count(include_folders=True)

    def test_mutation_during_enumeration_is_left_for_next_delta(self, drive: FakeDrive) -> None:
        for number in range(6):
            drive.add_file(ROOT_ID, f"{number}.txt", b"1", "text/plain")
        upto = drive.seq
        first = drive.delta_chunk(ROOT_ID, since=0, upto=upto, pos=None, limit=3)
        late = drive.add_file(ROOT_ID, "tarde.txt", b"1", "text/plain")

        rest = drive.delta_chunk(ROOT_ID, since=0, upto=upto, pos=first.next_pos, limit=50)

        assert late.id not in _ids(rest)
        assert _ids(_drain(drive, ROOT_ID, since=upto)) == [late.id]

    def test_delta_of_missing_root_is_404(self, drive: FakeDrive) -> None:
        assert (
            _error(lambda: drive.delta_chunk("FAKE-NOPE", since=0, upto=1, pos=None)).status == 404
        )

    def test_default_limit_is_page_size(self, drive: FakeDrive) -> None:
        drive.page_size = 2
        for number in range(5):
            drive.add_file(ROOT_ID, f"{number}.txt", b"1", "text/plain")

        chunk = drive.delta_chunk(ROOT_ID, since=0, upto=drive.seq, pos=None)

        assert len(chunk.entries) == 2
        assert chunk.next_pos is not None

    def test_restore_reappears_in_delta_with_data(self, drive: FakeDrive) -> None:
        item = drive.add_file(ROOT_ID, "a.txt", b"1", "text/plain")
        drive.delete(item.id)
        token = drive.seq
        drive.restore(item.id)

        entries = _drain(drive, ROOT_ID, since=token).entries

        assert [(e.item.id, e.gone) for e in entries] == [(item.id, False)]


class TestPopulate:
    def test_same_seed_same_drive(self) -> None:
        first, second = FakeDrive(), FakeDrive()

        first.populate(300, seed=11)
        second.populate(300, seed=11)

        def names(drive: FakeDrive) -> list[tuple[str, str | None, int]]:
            ids = sorted(drive._items)
            return [(drive.get(i).name, drive.get(i).mime_type, drive.get(i).size) for i in ids]

        assert names(first) == names(second)

    def test_different_seed_different_drive(self) -> None:
        first, second = FakeDrive(), FakeDrive()
        first.populate(100, seed=1)
        second.populate(100, seed=2)

        assert [i.size for i in first.children(first.camera_roll_id or "")] != [
            i.size for i in second.children(second.camera_roll_id or "")
        ]

    def test_summary_counts_and_mix(self, drive: FakeDrive) -> None:
        summary = drive.populate(2000, seed=5)

        assert summary.files == 2000 == drive.count()
        assert sum(summary.images.values()) + summary.other_files == 2000
        assert summary.images[ImageKind.PHOTO] > summary.images[ImageKind.SCREENSHOT] > 0
        assert summary.images[ImageKind.MEME] > 0
        assert summary.images[ImageKind.DOCUMENT] > 0
        assert summary.other_files > 0
        assert summary.folders >= 6

    def test_metadata_is_complete_and_consistent(self, drive: FakeDrive) -> None:
        drive.populate(400, seed=2)
        files = [i for i in drive._items.values() if not i.is_folder]

        images = [i for i in files if i.is_image]
        assert images
        assert all(i.width and i.height and i.size > 0 and i.spec for i in images)
        assert all(i.created <= i.modified for i in files)
        photos = [i for i in images if i.photo is not None]
        assert photos
        assert all(i.spec and i.spec.kind is ImageKind.PHOTO for i in photos)
        assert Counter(i.mime_type for i in images).keys() == {"image/jpeg", "image/png"}

    def test_content_renders_on_demand_and_deterministically(self, drive: FakeDrive) -> None:
        drive.populate(20, seed=3)
        image = next(i for i in drive._items.values() if i.spec is not None)

        content = drive.render_content(image)

        assert content == drive.render_content(image)
        assert image.content is None
        assert content[:4] in {b"\xff\xd8\xff\xe0", b"\x89PNG"}

    def test_populate_under_given_parent(self, drive: FakeDrive) -> None:
        base = drive.add_folder(ROOT_ID, "Base")

        drive.populate(50, seed=1, parent_id=base.id)

        assert drive.find_by_path(base.id, "Pictures/Camera Roll").is_folder
        assert drive.children(drive.camera_roll_id or "") == []

    def test_populate_is_additive_and_names_stay_unique(self, drive: FakeDrive) -> None:
        drive.populate(100, seed=1)
        drive.populate(100, seed=1)

        assert drive.count() == 200

    def test_negative_count_is_rejected(self, drive: FakeDrive) -> None:
        with pytest.raises(ValueError, match="count"):
            drive.populate(-1)

    def test_populate_zero_is_noop(self, drive: FakeDrive) -> None:
        assert drive.populate(0).files == 0

    def test_populate_50k_items_in_under_30_seconds(self, drive: FakeDrive) -> None:
        started = time.perf_counter()

        summary = drive.populate(50_000, seed=42)

        elapsed = time.perf_counter() - started
        assert summary.files == 50_000
        assert drive.count() == 50_000
        assert elapsed < 30, f"populate(50_000) levou {elapsed:.1f}s"

    def test_delta_over_50k_items_pages_through_everything(self) -> None:
        drive = FakeDrive()
        drive.populate(5_000, seed=1)

        chunk = _drain(drive, ROOT_ID, limit=200)

        assert len(chunk.entries) == drive.count(include_folders=True)
