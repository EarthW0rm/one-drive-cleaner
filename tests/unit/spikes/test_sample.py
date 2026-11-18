# SPDX-License-Identifier: GPL-3.0-only
"""Testes do `sample`: Graph fake in-process, auth fake e diretórios temporários (sem rede)."""

import ast
import io
import json
import re
from collections import Counter
from pathlib import Path

import pytest
from PIL import Image
from spikes.embedding_benchmark import sample
from spikes.embedding_benchmark.errors import BenchmarkError, LoginNeededError
from spikes.embedding_benchmark.manifest import Manifest, load_manifest, manifest_path
from spikes.embedding_benchmark.sample import Candidate, SampleConfig

from odc.auth.errors import KeychainUnavailableError, LoginRequired, TokenRefreshError
from odc.graph.client import GraphClient
from odc.graph.errors import GraphClientError
from odc.graph.models import DriveItem
from odc.telemetry.metrics import GraphOperation, LoginReason
from tests.fakes.faults import expire_token, fail_network, not_found
from tests.fakes.graph_fake import GraphFake
from tests.fakes.graph_state import FakeDrive
from tests.fakes.synthetic_images import ImageFormat, ImageKind, make_spec, render
from tests.unit.spikes.support import FakeAuth, expired_auth, fill_drive, graph_session

SPIKE_SOURCE = Path(sample.__file__)
COUNTS = {"screenshot": 8, "whatsapp": 7, "camera": 9, "other": 6}


def _item(**overrides: object) -> DriveItem:
    raw: dict[str, object] = {
        "id": "FAKE-1",
        "name": "arquivo.jpg",
        "file": {"mimeType": "image/jpeg"},
        "image": {"width": 4000, "height": 3000},
    }
    return DriveItem.model_validate({**raw, **overrides})


def _candidate(number: int, stratum: str = "other") -> Candidate:
    flags = {"name_screenshot": ("screenshot",), "name_whatsapp": ("whatsapp",)}.get(stratum, ())
    return Candidate(
        drive_item_id=f"FAKE-{number:05d}",
        mime_type="image/jpeg",
        width=100,
        height=100,
        has_camera=stratum == "has_camera",
        has_taken_at=False,
        name_flags=flags,  # type: ignore[arg-type]
    )


def _run(
    fake: GraphFake,
    out: Path,
    *,
    auth: FakeAuth | None = None,
    **config: object,
) -> tuple[int, str]:
    stdout = io.StringIO()
    settings = SampleConfig(out=out, sizes=("c32x32", "large"), **config)  # type: ignore[arg-type]
    with fake.activate():
        code = sample.run_sample(settings, auth or FakeAuth(), graph_session, stdout=stdout)
    return code, stdout.getvalue()


def _fresh() -> tuple[GraphFake, list[str]]:
    drive, names = fill_drive(COUNTS)
    return GraphFake(drive), names


def _thumb_calls(fake: GraphFake) -> list[object]:
    return [c for c in fake.calls if c.op is GraphOperation.THUMBNAIL_CONTENT]


def _seq_files(out: Path) -> list[Path]:
    return sorted(p for p in out.rglob("*.jpg"))


# --- Parâmetros ---


def test_parse_thumb_sizes_aceita_nomeados_e_customizados() -> None:
    assert sample.parse_thumb_sizes("c384x384, large,small,large") == ("c384x384", "large", "small")


@pytest.mark.parametrize("text", ["", "c384x384", "huge,large", "c384,large", "large,c1x"])
def test_parse_thumb_sizes_rejeita_invalidos_e_exige_large(text: str) -> None:
    with pytest.raises(BenchmarkError, match=r"tamanhos|large"):
        sample.parse_thumb_sizes(text)


# --- Candidatos ---


def test_candidate_from_classifica_sinais_sem_guardar_o_nome() -> None:
    candidate = sample.candidate_from(
        _item(
            name="Screenshot_2024.png",
            photo={"cameraMake": "X", "takenDateTime": "2024-01-01T00:00:00Z"},
        )
    )

    assert candidate is not None
    assert candidate.name_flags == ("screenshot",)
    assert candidate.has_camera is True
    assert candidate.has_taken_at is True
    assert "Screenshot" not in repr(candidate)


@pytest.mark.parametrize(
    ("name", "flag"),
    [("IMG-20240101-WA0001.jpg", "whatsapp"), ("PXL_2024.jpg", "camera")],
)
def test_candidate_from_reconhece_whatsapp_e_camera(name: str, flag: str) -> None:
    candidate = sample.candidate_from(_item(name=name))

    assert candidate is not None
    assert candidate.name_flags == (flag,)


@pytest.mark.parametrize(
    "raw",
    [
        {"deleted": {}},
        {"folder": {"childCount": 1}, "file": None},
        {"file": {"mimeType": "application/pdf"}},
        {"file": {}},
        {"file": None},
    ],
)
def test_candidate_from_ignora_o_que_nao_e_imagem(raw: dict[str, object]) -> None:
    assert sample.candidate_from(_item(**raw)) is None


def test_candidate_from_trata_item_sem_nome_nem_facetas() -> None:
    candidate = sample.candidate_from(
        DriveItem.model_validate({"id": "FAKE-2", "file": {"mimeType": "image/heic"}})
    )

    assert candidate is not None
    assert (candidate.width, candidate.height, candidate.has_camera) == (None, None, False)
    assert candidate.suggested == "_revisar"


@pytest.mark.parametrize(
    ("kwargs", "stratum", "suggested"),
    [
        ({"name_flags": ("screenshot",)}, "name_screenshot", "screenshot"),
        (
            {"name_flags": ("whatsapp",), "width": 1080, "height": 2340},
            "name_whatsapp",
            "screenshot",
        ),
        ({"has_camera": True, "has_taken_at": True}, "has_camera", "real_photo"),
        ({"has_camera": True, "has_taken_at": False}, "has_camera", "_revisar"),
        (
            {"name_flags": ("camera",), "has_camera": True, "has_taken_at": True},
            "has_camera",
            "real_photo",
        ),
        ({"width": 1080, "height": 2340, "has_camera": True}, "has_camera", "_revisar"),
        ({"width": 2340, "height": 1080}, "other", "screenshot"),
        ({"name_flags": ("whatsapp",)}, "name_whatsapp", "_revisar"),
        ({}, "other", "_revisar"),
    ],
)
def test_candidate_estrato_e_sugestao(
    kwargs: dict[str, object], stratum: str, suggested: str
) -> None:
    base: dict[str, object] = {
        "drive_item_id": "FAKE-1",
        "mime_type": "image/jpeg",
        "width": 100,
        "height": 100,
        "has_camera": False,
        "has_taken_at": False,
        "name_flags": (),
    }

    candidate = Candidate(**{**base, **kwargs})  # type: ignore[arg-type]

    assert candidate.stratum == stratum
    assert candidate.suggested == suggested


# --- Cotas e seleção ---


def test_allocate_distribui_o_resto_proporcionalmente() -> None:
    quotas = sample.allocate({"a": 500, "b": 300, "c": 100, "d": 100}, 400)

    assert sum(quotas.values()) == 400
    assert all(q >= 60 for q in quotas.values())
    assert quotas["a"] > quotas["b"] > quotas["c"]


def test_allocate_garante_minimo_e_leva_todos_quando_ha_menos() -> None:
    quotas = sample.allocate({"a": 900, "b": 10, "c": 70}, 100)

    assert quotas["b"] == 10
    assert quotas["c"] >= 60
    assert sum(quotas.values()) >= 100


def test_allocate_minimos_acima_do_tamanho_prevalecem() -> None:
    quotas = sample.allocate({"a": 500, "b": 500}, 50)

    assert quotas == {"a": 60, "b": 60}


def test_allocate_pega_tudo_quando_o_total_e_menor_que_o_pedido() -> None:
    available = {"a": 100, "b": 30, "c": 0}

    assert sample.allocate(available, 400) == available


def test_select_sample_respeita_cotas_e_e_reprodutivel_pela_seed() -> None:
    pool = [_candidate(n, s) for n, s in enumerate(["name_screenshot"] * 300 + ["other"] * 200)]
    config = SampleConfig(out=Path("."), size=200, seed=7)

    first = sample.select_sample(pool, config)
    again = sample.select_sample(list(reversed(pool)), config)
    other_seed = sample.select_sample(pool, SampleConfig(out=Path("."), size=200, seed=8))

    assert [c.drive_item_id for c in first] == [c.drive_item_id for c in again]
    assert [c.drive_item_id for c in first] != [c.drive_item_id for c in other_seed]
    assert Counter(c.stratum for c in first) == {"name_screenshot": 111, "other": 89}
    assert len({c.drive_item_id for c in first}) == 200


# --- Coleta com o Graph fake ---


def test_sample_grava_arquivos_anonimizados_e_manifesto_sem_nomes(tmp_path: Path) -> None:
    fake, names = _fresh()
    out = tmp_path / "data"

    code, output = _run(fake, out, size=40, seed=42)

    assert code == 0
    files = _seq_files(out)
    assert files
    assert all(re.fullmatch(r"\d{6}\.jpg", path.name) for path in files)
    everything = [p.name for p in out.rglob("*")] + [
        manifest_path(out).read_text(encoding="utf-8"),
        output,
    ]
    assert not any(name.split(".")[0] in text for name in names for text in everything)
    manifest = load_manifest(out)
    assert manifest is not None
    assert manifest.folder == "camera_roll"
    assert len(manifest.items) == sum(COUNTS.values())  # 30 < 40: todas as imagens
    assert {item.seq for item in manifest.items} == {f"{n:06d}" for n in range(1, 31)}
    for item in manifest.items:
        assert item.sizes == ("c32x32", "large")
        assert (out / "_thumbs" / "large" / f"{item.seq}.jpg").is_file()
        assert (out / "_thumbs" / "c32x32" / f"{item.seq}.jpg").is_file()


def test_sample_pre_classifica_a_versao_large_nas_pastas_sugeridas(tmp_path: Path) -> None:
    fake, _ = _fresh()
    out = tmp_path / "data"

    _run(fake, out, size=40)

    manifest = load_manifest(out)
    assert manifest is not None
    for item in manifest.items:
        placed = out / item.suggested / f"{item.seq}.jpg"
        assert placed.read_bytes() == (out / "_thumbs" / "large" / f"{item.seq}.jpg").read_bytes()
    suggested = Counter(item.suggested for item in manifest.items)
    assert suggested["screenshot"] == COUNTS["screenshot"]
    assert suggested["real_photo"] == COUNTS["camera"]
    assert suggested["_revisar"] == COUNTS["whatsapp"] + COUNTS["other"]
    assert not (out / "meme_sticker").exists()


def test_sample_registra_somente_leitura_na_graph(tmp_path: Path) -> None:
    fake, _ = _fresh()

    _run(fake, tmp_path / "data", size=10)

    assert {call.method for call in fake.calls} == {"GET"}
    assert all(not call.path.endswith("/$batch") for call in fake.calls)
    assert fake.drive.recycle_bin() == []


def test_sample_nao_importa_escrita_da_graph_nem_o_banco_da_aplicacao() -> None:
    tree = ast.parse(SPIKE_SOURCE.read_text(encoding="utf-8"))
    imports = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert not any(module.startswith("odc.storage") for module in imports)
    assert called.isdisjoint({"ensure_folder", "move", "delete", "download_content"})


def test_sample_e_reprodutivel_pela_seed(tmp_path: Path) -> None:
    drive, _ = fill_drive({"screenshot": 30, "camera": 30, "other": 30})
    ids = []
    for run in ("a", "b"):
        _run(GraphFake(drive), tmp_path / run, size=20, seed=5)
        manifest = load_manifest(tmp_path / run)
        assert manifest is not None
        ids.append([(item.seq, item.drive_item_id) for item in manifest.items])

    assert ids[0] == ids[1]
    assert len(ids[0]) == 90  # mínimo de 60 por estrato: todos os 30 de cada um entram


def test_sample_com_menos_imagens_que_o_pedido_baixa_todas(tmp_path: Path) -> None:
    drive, _ = fill_drive({"camera": 5})

    code, output = _run(GraphFake(drive), tmp_path / "data", size=400)

    manifest = load_manifest(tmp_path / "data")
    assert code == 0
    assert manifest is not None
    assert len(manifest.items) == 5
    assert "Selecionadas: 5" in output


def test_sample_ignora_arquivos_que_nao_sao_imagem(tmp_path: Path) -> None:
    drive, _ = fill_drive({"camera": 4})
    assert drive.camera_roll_id is not None
    drive.add_file(drive.camera_roll_id, "nota.pdf", b"%PDF", "application/pdf")
    drive.add_folder(drive.camera_roll_id, "subpasta")

    _run(GraphFake(drive), tmp_path / "data", size=400)

    manifest = load_manifest(tmp_path / "data")
    assert manifest is not None
    assert len(manifest.items) == 4


def test_sample_usa_a_pasta_informada_e_ignora_removidos(tmp_path: Path) -> None:
    drive = FakeDrive()
    folder = drive.add_folder(drive.root_id, "Outra")
    keep = drive.add_image(folder.id, "keep.jpg", ImageKind.PHOTO, width=32, height=32)
    gone = drive.add_image(folder.id, "gone.jpg", ImageKind.PHOTO, seed=2, width=32, height=32)
    drive.delete(gone.id)

    _run(GraphFake(drive), tmp_path / "data", folder_id=folder.id)

    manifest = load_manifest(tmp_path / "data")
    assert manifest is not None
    assert manifest.folder == folder.id
    assert [item.drive_item_id for item in manifest.items] == [keep.id]


# --- tamanho customizado ---


def test_sample_registra_custom_size_suportado(tmp_path: Path) -> None:
    fake, _ = _fresh()

    _, output = _run(fake, tmp_path / "data", size=6)

    manifest = load_manifest(tmp_path / "data")
    assert manifest is not None
    assert manifest.custom_size_supported is True
    assert "customizado suportado pela Graph: sim" in output
    with Image.open(tmp_path / "data" / "_thumbs" / "c32x32" / "000001.jpg") as thumb:
        assert max(thumb.size) <= 32


def test_sample_gera_o_tamanho_customizado_da_large_quando_a_graph_recusa(
    tmp_path: Path,
) -> None:
    drive, _ = fill_drive({"camera": 6})
    fake = GraphFake(drive)
    out = tmp_path / "data"
    stdout = io.StringIO()
    config = SampleConfig(out=out, sizes=("c2000x2000", "large"))

    with fake.activate():
        sample.run_sample(config, FakeAuth(), graph_session, stdout=stdout)

    manifest = load_manifest(out)
    assert manifest is not None
    assert manifest.custom_size_supported is False
    assert len(manifest.items) == 6
    with Image.open(out / "_thumbs" / "c2000x2000" / "000001.jpg") as derived:
        assert derived.size == (2000, 2000)
    assert "customizado suportado pela Graph: não" in stdout.getvalue()


def test_sample_sem_tamanho_customizado_deixa_o_campo_nulo(tmp_path: Path) -> None:
    drive, _ = fill_drive({"camera": 3})
    config = SampleConfig(out=tmp_path / "data", sizes=("large",))

    with GraphFake(drive).activate():
        sample.run_sample(config, FakeAuth(), graph_session, stdout=io.StringIO())

    manifest = load_manifest(tmp_path / "data")
    assert manifest is not None
    assert manifest.custom_size_supported is None


def test_normalize_jpeg_converte_miniatura_que_nao_e_jpeg(tmp_path: Path) -> None:
    png = make_spec(ImageKind.SCREENSHOT, 1, width=40, height=40, image_format=ImageFormat.PNG)

    data = sample.normalize_jpeg(render(png))

    assert data[:2] == b"\xff\xd8"
    assert sample.normalize_jpeg(data) == data


def test_normalize_jpeg_rejeita_bytes_que_nao_sao_imagem() -> None:
    with pytest.raises(BenchmarkError, match="inválida"):
        sample.normalize_jpeg(b"<html>erro</html>")


def test_crop_to_size_exige_tamanho_customizado() -> None:
    jpeg = sample.normalize_jpeg(render(make_spec(ImageKind.PHOTO, 1, width=40, height=30)))

    with pytest.raises(BenchmarkError, match="customizado"):
        sample.crop_to_size(jpeg, "large")
    with Image.open(io.BytesIO(sample.crop_to_size(jpeg, "c10x10"))) as cropped:
        assert cropped.size == (10, 10)


# --- Retomada, interrupção e falhas ---


def test_reexecucao_com_a_mesma_seed_nao_baixa_novamente(tmp_path: Path) -> None:
    fake, _ = _fresh()
    out = tmp_path / "data"
    _run(fake, out, size=40)
    manifest_before = manifest_path(out).read_text(encoding="utf-8")
    thumbs_before = len(_thumb_calls(fake))

    _, output = _run(fake, out, size=40)

    assert len(_thumb_calls(fake)) == thumbs_before
    assert manifest_path(out).read_text(encoding="utf-8") == manifest_before
    assert "baixadas agora: 0; já existentes: 30" in output


def test_retomada_respeita_o_que_o_utilizador_ja_reorganizou(tmp_path: Path) -> None:
    fake, _ = _fresh()
    out = tmp_path / "data"
    _run(fake, out, size=40)
    manifest = load_manifest(out)
    assert manifest is not None
    review = next(i for i in manifest.items if i.suggested == "_revisar")
    other = next(i for i in manifest.items if i.suggested == "screenshot")
    (out / "meme_sticker").mkdir()
    (out / "_revisar" / f"{review.seq}.jpg").rename(out / "meme_sticker" / f"{review.seq}.jpg")
    (out / "screenshot" / f"{other.seq}.jpg").unlink()

    _run(fake, out, size=40)

    assert (out / "meme_sticker" / f"{review.seq}.jpg").is_file()
    assert not (out / "_revisar" / f"{review.seq}.jpg").exists()
    assert not (out / "screenshot" / f"{other.seq}.jpg").exists()


def test_retomada_apos_falha_completa_so_o_que_faltou(tmp_path: Path) -> None:
    fake, _ = _fresh()
    out = tmp_path / "data"
    fake.faults.add(fail_network(GraphOperation.THUMBNAIL_CONTENT, times=None))
    _, first = _run(fake, out, size=40)
    fake.faults.clear()
    assert "puladas por erro: 30" in first
    assert load_manifest(out) is None or not load_manifest(out).items  # type: ignore[union-attr]

    _, second = _run(fake, out, size=40)

    manifest = load_manifest(out)
    assert manifest is not None
    assert len(manifest.items) == 30
    assert "puladas por erro: 0" in second
    assert {p.stem for p in (out / "_thumbs" / "large").iterdir()} == {
        i.seq for i in manifest.items
    }


def test_retomada_remove_arquivos_orfaos_sem_entrada_no_manifesto(tmp_path: Path) -> None:
    fake, _ = _fresh()
    out = tmp_path / "data"
    _run(fake, out, size=10)
    orphan_thumb = out / "_thumbs" / "large" / "000999.jpg"
    orphan_placed = out / "_revisar" / "000998.jpg"
    keep = out / "_revisar" / "minha-foto.jpg"
    for path in (orphan_thumb, orphan_placed, keep):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    _run(fake, out, size=10)

    assert not orphan_thumb.exists()
    assert not orphan_placed.exists()
    assert keep.exists()


def test_retomada_baixa_tamanho_novo_sem_recolocar_a_sugestao(tmp_path: Path) -> None:
    fake, _ = _fresh()
    out = tmp_path / "data"
    _run(fake, out, size=40)
    before = sorted(p.name for p in (out / "_revisar").iterdir())
    stdout = io.StringIO()
    config = SampleConfig(out=out, sizes=("c32x32", "large", "medium"))

    with fake.activate():
        sample.run_sample(config, FakeAuth(), graph_session, stdout=stdout)

    manifest = load_manifest(out)
    assert manifest is not None
    assert all(item.sizes == ("c32x32", "large", "medium") for item in manifest.items)
    assert (out / "_thumbs" / "medium" / "000001.jpg").is_file()
    assert sorted(p.name for p in (out / "_revisar").iterdir()) == before


def test_retomada_rebaixa_a_large_removida_do_disco(tmp_path: Path) -> None:
    fake, _ = _fresh()
    out = tmp_path / "data"
    _run(fake, out, size=10)
    (out / "_thumbs" / "large" / "000001.jpg").unlink()

    _, output = _run(fake, out, size=10)

    assert (out / "_thumbs" / "large" / "000001.jpg").is_file()
    assert "baixadas agora: 1" in output


def test_retomada_com_outra_seed_ou_pasta_e_recusada(tmp_path: Path) -> None:
    fake, _ = _fresh()
    out = tmp_path / "data"
    _run(fake, out, size=10, seed=1)

    with pytest.raises(BenchmarkError, match="outra seed ou pasta"):
        _run(fake, out, size=10, seed=2)


def test_item_que_falha_e_pulado_e_a_amostra_segue(tmp_path: Path) -> None:
    drive, _ = fill_drive({"camera": 6})
    fake = GraphFake(drive)
    victim = drive.children(drive.camera_roll_id or "")[0]
    fake.faults.add(not_found(victim.id))

    _, output = _run(fake, tmp_path / "data", size=6)

    manifest = load_manifest(tmp_path / "data")
    assert manifest is not None
    assert victim.id not in {item.drive_item_id for item in manifest.items}
    assert len(manifest.items) == 5
    assert "puladas por erro: 1" in output


def test_miniatura_invalida_conta_como_pulada(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive, _ = fill_drive({"camera": 3})
    monkeypatch.setattr(sample, "normalize_jpeg", _raise_invalid)

    _, output = _run(GraphFake(drive), tmp_path / "data", size=3)

    assert "puladas por erro: 3" in output


def _raise_invalid(data: bytes) -> bytes:
    del data
    message = "miniatura inválida"
    raise BenchmarkError(message)


# --- Autenticação e erros da Graph ---


def test_sem_conta_ativa_instrui_login_e_nao_cria_arquivos(tmp_path: Path) -> None:
    fake, _ = _fresh()
    out = tmp_path / "data"

    with pytest.raises(LoginNeededError, match="execute `odc`"):
        _run(fake, out, auth=FakeAuth(account=False))

    assert not out.exists()
    assert fake.calls == []


def test_login_expirado_na_listagem_instrui_login_sem_arquivo_parcial(tmp_path: Path) -> None:
    fake, _ = _fresh()
    out = tmp_path / "data"

    with pytest.raises(LoginNeededError, match="sessão expirada"):
        _run(fake, out, auth=expired_auth())

    assert not out.exists()


def test_401_repetido_vira_login_necessario(tmp_path: Path) -> None:
    fake, _ = _fresh()
    fake.faults.add(expire_token(times=None))

    with pytest.raises(LoginNeededError):
        _run(fake, tmp_path / "data")


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (TokenRefreshError("rede"), "Graph indisponível"),  # transitória: retentativas esgotadas
        (KeychainUnavailableError("kc"), "falha de autenticação"),
    ],
)
def test_falha_de_autenticacao_vira_erro_do_benchmark(
    tmp_path: Path, error: Exception, message: str
) -> None:
    fake, _ = _fresh()

    with pytest.raises(BenchmarkError, match=message):
        _run(fake, tmp_path / "data", auth=FakeAuth(error=error))


def test_graph_indisponivel_na_listagem_vira_erro_do_benchmark(tmp_path: Path) -> None:
    fake, _ = _fresh()
    fake.faults.add(fail_network(GraphOperation.DELTA, times=None))

    with pytest.raises(BenchmarkError, match="Graph indisponível"):
        _run(fake, tmp_path / "data")


def test_pasta_inexistente_vira_erro_do_benchmark(tmp_path: Path) -> None:
    fake, _ = _fresh()

    with pytest.raises(BenchmarkError, match=r"recusou a listagem \(404"):
        _run(fake, tmp_path / "data", folder_id="FAKE-INEXISTENTE")


def test_pasta_sem_imagens_e_tamanho_invalido(tmp_path: Path) -> None:
    empty = GraphFake(FakeDrive())

    with pytest.raises(BenchmarkError, match="nenhuma imagem"):
        _run(empty, tmp_path / "data")
    with pytest.raises(BenchmarkError, match="--size"):
        _run(empty, tmp_path / "data", size=0)


def test_manifesto_corrompido_e_recusado(tmp_path: Path) -> None:
    fake, _ = _fresh()
    out = tmp_path / "data"
    out.mkdir()
    manifest_path(out).write_text("{nao e json", encoding="utf-8")

    with pytest.raises(BenchmarkError, match=r"manifest\.local\.json"):
        _run(fake, out)


def test_manifesto_gravado_tem_permissao_restrita_e_formato_do_contrato(tmp_path: Path) -> None:
    fake, _ = _fresh()
    out = tmp_path / "data"
    _run(fake, out, size=5)

    raw = json.loads(manifest_path(out).read_text(encoding="utf-8"))

    assert set(raw) == {"seed", "folder", "custom_size_supported", "items"}
    assert set(raw["items"][0]) == {
        "seq", "drive_item_id", "mime_type", "width", "height", "has_camera",
        "has_taken_at", "name_flags", "sizes", "suggested",
    }  # fmt: skip
    assert manifest_path(out).stat().st_mode & 0o777 == 0o600
    assert Manifest.model_validate(raw).seed == 42


def test_sample_usa_o_cliente_real_da_graph_por_padrao(tmp_path: Path) -> None:
    drive, _ = fill_drive({"camera": 3})
    config = SampleConfig(out=tmp_path / "data", sizes=("large",))

    with GraphFake(drive).activate():
        code = sample.run_sample(config, FakeAuth(), stdout=io.StringIO())

    manifest = load_manifest(tmp_path / "data")
    assert code == 0
    assert manifest is not None
    assert len(manifest.items) == 3


def test_resposta_invalida_do_tamanho_customizado_pula_o_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive, _ = fill_drive({"camera": 3})
    original = GraphClient.get_thumbnail_bytes

    async def flaky(self: GraphClient, item_id: str, size: str) -> bytes:
        if size.startswith("c"):
            raise GraphClientError(200, "invalid_response")
        return await original(self, item_id, size)

    monkeypatch.setattr(GraphClient, "get_thumbnail_bytes", flaky)

    _, output = _run(GraphFake(drive), tmp_path / "data", size=3)

    assert "puladas por erro: 3" in output
    assert "customizado suportado pela Graph: não verificado" in output


def test_login_que_expira_durante_o_download_encerra_sem_arquivo_parcial(tmp_path: Path) -> None:
    fake, _ = _fresh()
    out = tmp_path / "data"
    auth = FakeAuth(error=LoginRequired(LoginReason.EXPIRED), fail_after=4)

    with pytest.raises(LoginNeededError, match="sessão expirada"):
        _run(fake, out, auth=auth, size=40)

    assert not list(out.rglob("*.tmp"))
    manifest = load_manifest(out)
    stored = {item.seq for item in manifest.items} if manifest else set()
    placed = {
        path.stem
        for folder in ("screenshot", "real_photo", "_revisar")
        for path in (out / folder).glob("*.jpg")
    }
    assert placed <= stored  # nada colocado para revisão sem entrada íntegra no manifesto
    assert all(path.stat().st_size > 0 for path in out.rglob("*.jpg"))
