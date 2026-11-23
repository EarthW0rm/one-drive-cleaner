# SPDX-License-Identifier: GPL-3.0-only
import logging
import threading
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from odc import paths
from odc.features.embedder import (
    BATCH_SIZE,
    Embedder,
    EmbedderOptions,
    OpenClipEmbedder,
    l2_normalize,
    to_rgb,
)
from odc.features.errors import (
    EmbedderConfigError,
    EmbeddingError,
    WeightsUnavailableError,
)
from odc.telemetry.metrics import Device
from tests.unit.features.clip_fakes import DIM, FakeStack, Faults
from tests.unit.features.embedder_contract import (
    assert_batch_equals_single,
    assert_deterministic,
    assert_image_contract,
    assert_text_contract,
    row_cosines,
)

MODEL = "ViT-B-32"
PRETRAINED = "laion2b_s34b_b79k"


class RecordingMetrics:
    def __init__(self) -> None:
        self.batches: list[tuple[Device, int, float]] = []

    def record_embedding_batch(self, *, device: Device, items: int, duration_s: float) -> None:
        self.batches.append((device, items, duration_s))


class StepClock:
    """Relógio determinístico: cada leitura avança 0,5 s."""

    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        self._now += 0.5
        return self._now


def _image(color: tuple[int, int, int], size: int = 12) -> Image.Image:
    return Image.new("RGB", (size, size), color)


def _images(count: int) -> list[Image.Image]:
    return [
        _image((20 + index * 37 % 200, 20 + index * 91 % 200, 20 + index * 13 % 200))
        for index in range(count)
    ]


def _embedder(
    stack: FakeStack,
    tmp_path: Path,
    *,
    device: str = "auto",
    metrics: RecordingMetrics | None = None,
    expected_dim: int | None = None,
) -> OpenClipEmbedder:
    options = EmbedderOptions(
        expected_dim=expected_dim,
        metrics=metrics,
        models_dir=lambda: tmp_path / "models",
        import_module=stack.import_module,
        clock=StepClock(),
    )
    return OpenClipEmbedder(MODEL, PRETRAINED, device, options=options)


def test_contrato_de_imagem_valido_com_modelo_fake(tmp_path: Path) -> None:
    embedder = _embedder(FakeStack(), tmp_path)

    assert_image_contract(embedder, _images(5), DIM)


def test_embedder_e_deterministico(tmp_path: Path) -> None:
    assert_deterministic(_embedder(FakeStack(), tmp_path), _images(4))


def test_lote_equivale_a_imagens_individuais(tmp_path: Path) -> None:
    assert_batch_equals_single(_embedder(FakeStack(), tmp_path), _images(6))


def test_contrato_de_texto_valido_com_modelo_fake(tmp_path: Path) -> None:
    embedder = _embedder(FakeStack(), tmp_path)

    assert_text_contract(embedder, ["a screenshot", "a photo", "a receipt"], DIM)


def test_logit_scale_e_exp_do_parametro_do_modelo(tmp_path: Path) -> None:
    embedder = _embedder(FakeStack(), tmp_path)

    assert embedder.logit_scale == pytest.approx(100.0)


def test_imagens_diferentes_geram_vetores_diferentes(tmp_path: Path) -> None:
    embedder = _embedder(FakeStack(), tmp_path)

    vectors = embedder.embed_images([_image((250, 0, 0)), _image((0, 0, 250))])

    assert row_cosines(vectors[:1], vectors[1:])[0] < 0.999


def test_embedder_satisfaz_o_protocol() -> None:
    def accepts(embedder: Embedder) -> int:
        return embedder.dim

    stack = FakeStack()
    embedder = OpenClipEmbedder(
        MODEL, PRETRAINED, options=EmbedderOptions(import_module=stack.import_module)
    )

    assert accepts(embedder) == DIM


def test_construcao_nao_carrega_o_modelo(tmp_path: Path) -> None:
    stack = FakeStack()

    _embedder(stack, tmp_path)

    assert stack.imported == []


def test_modelo_e_carregado_uma_unica_vez(tmp_path: Path) -> None:
    stack = FakeStack()
    embedder = _embedder(stack, tmp_path)

    embedder.embed_images(_images(2))
    embedder.embed_texts(["x"])
    _ = embedder.dim

    assert len(stack.open_clip.create_calls) == 1


def test_carga_concorrente_acontece_uma_unica_vez(tmp_path: Path) -> None:
    stack = FakeStack()
    embedder = _embedder(stack, tmp_path)
    barrier = threading.Barrier(8)

    def work() -> None:
        barrier.wait()
        embedder.embed_images(_images(1))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(stack.open_clip.create_calls) == 1


def test_pesos_sao_lidos_e_gravados_no_diretorio_de_modelos(tmp_path: Path) -> None:
    stack = FakeStack()

    _embedder(stack, tmp_path).embed_images(_images(1))

    call = stack.open_clip.create_calls[0]
    assert (call.name, call.pretrained) == (MODEL, PRETRAINED)
    assert call.cache_dir == str(tmp_path / "models")
    assert stack.open_clip.tokenizer_cache_dirs == [str(tmp_path / "models")]


def test_diretorio_de_modelos_padrao_e_odc_paths_models_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "models_dir", lambda: tmp_path / "padrao")
    stack = FakeStack()
    embedder = OpenClipEmbedder(
        MODEL, PRETRAINED, options=EmbedderOptions(import_module=stack.import_module)
    )

    embedder.embed_images(_images(1))

    assert stack.open_clip.create_calls[0].cache_dir == str(tmp_path / "padrao")


def test_modelo_e_colocado_em_modo_de_inferencia(tmp_path: Path) -> None:
    stack = FakeStack()
    embedder = _embedder(stack, tmp_path)

    embedder.embed_images(_images(1))

    assert embedder._loaded is not None
    assert embedder._loaded.model.eval_called is True  # type: ignore[attr-defined]


def test_lotes_de_ate_32_imagens_registram_metricas_por_lote(tmp_path: Path) -> None:
    metrics = RecordingMetrics()
    embedder = _embedder(FakeStack(), tmp_path, metrics=metrics)

    vectors = embedder.embed_images(_images(BATCH_SIZE * 2 + 6))

    assert vectors.shape == (70, DIM)
    assert metrics.batches == [
        (Device.CPU, 32, 0.5),
        (Device.CPU, 32, 0.5),
        (Device.CPU, 6, 0.5),
    ]


def test_sem_metricas_a_inferencia_funciona(tmp_path: Path) -> None:
    embedder = _embedder(FakeStack(), tmp_path, metrics=None)

    assert embedder.embed_images(_images(2)).shape == (2, DIM)


def test_lista_vazia_de_imagens_devolve_matriz_vazia_float16(tmp_path: Path) -> None:
    embedder = _embedder(FakeStack(), tmp_path)

    vectors = embedder.embed_images([])

    assert vectors.shape == (0, DIM)
    assert vectors.dtype == np.float16


def test_lista_vazia_de_textos_devolve_matriz_vazia_float32(tmp_path: Path) -> None:
    embedder = _embedder(FakeStack(), tmp_path)

    vectors = embedder.embed_texts([])

    assert vectors.shape == (0, DIM)
    assert vectors.dtype == np.float32


def test_textos_acima_de_32_sao_processados_em_lotes(tmp_path: Path) -> None:
    embedder = _embedder(FakeStack(), tmp_path)

    vectors = embedder.embed_texts([f"texto {index}" for index in range(40)])

    assert vectors.shape == (40, DIM)


@pytest.mark.parametrize("mode", ["CMYK", "P", "LA", "RGBA", "L", "1"])
def test_imagem_em_modo_incomum_e_convertida_para_rgb(tmp_path: Path, mode: str) -> None:
    stack = FakeStack()
    embedder = _embedder(stack, tmp_path)
    image = Image.new(mode, (10, 10))

    vectors = embedder.embed_images([image])

    assert vectors.shape == (1, DIM)
    assert stack.seen_modes == ["RGB"]


def test_to_rgb_mantem_imagem_rgb_sem_copia() -> None:
    image = _image((1, 2, 3))

    assert to_rgb(image) is image


def test_to_rgb_compoe_transparencia_sobre_fundo_branco() -> None:
    image = Image.new("RGBA", (2, 2), (255, 0, 0, 0))

    converted = to_rgb(image)

    assert converted.mode == "RGB"
    assert converted.getpixel((0, 0)) == (255, 255, 255)


def test_to_rgb_compoe_paleta_com_transparencia() -> None:
    image = Image.new("P", (2, 2), 0)
    image.putpalette([255, 0, 0] + [0, 0, 0] * 255)
    image.info["transparency"] = 0

    converted = to_rgb(image)

    assert converted.getpixel((1, 1)) == (255, 255, 255)


def test_to_rgb_converte_cmyk() -> None:
    converted = to_rgb(Image.new("CMYK", (2, 2), (0, 0, 0, 0)))

    assert converted.mode == "RGB"
    assert converted.getpixel((0, 0)) == (255, 255, 255)


def test_l2_normalize_normaliza_linhas_e_preserva_linhas_nulas() -> None:
    vectors = np.array([[3.0, 4.0], [0.0, 0.0]], dtype=np.float32)

    normalized = l2_normalize(vectors)

    assert normalized[0].tolist() == pytest.approx([0.6, 0.8])
    assert normalized[1].tolist() == [0.0, 0.0]


@pytest.mark.parametrize(
    "name", ["", "../ViT", "/abs/path", "hf-hub:org/model", "http://x/y", "a b"]
)
def test_nome_de_modelo_invalido_e_rejeitado(name: str) -> None:
    with pytest.raises(EmbedderConfigError, match="nome do modelo"):
        OpenClipEmbedder(name, PRETRAINED)


@pytest.mark.parametrize(
    "pretrained", ["", "/pesos/modelo.pt", "https://host/pesos.bin", "a/b", "-inicio"]
)
def test_pretrained_invalido_e_rejeitado(pretrained: str) -> None:
    with pytest.raises(EmbedderConfigError, match="pretrained"):
        OpenClipEmbedder(MODEL, pretrained)


def test_dispositivo_invalido_e_rejeitado() -> None:
    with pytest.raises(EmbedderConfigError, match="dispositivo"):
        OpenClipEmbedder(MODEL, PRETRAINED, "cuda")


def test_falha_de_download_dos_pesos_vira_weights_unavailable(tmp_path: Path) -> None:
    stack = FakeStack(faults=Faults(weights_oserror=True))
    embedder = _embedder(stack, tmp_path)

    with pytest.raises(WeightsUnavailableError, match="pesos"):
        embedder.embed_images(_images(1))


def test_apos_falha_de_download_a_proxima_chamada_tenta_de_novo(tmp_path: Path) -> None:
    stack = FakeStack(faults=Faults(weights_oserror=True))
    embedder = _embedder(stack, tmp_path)
    with pytest.raises(WeightsUnavailableError):
        embedder.embed_images(_images(1))

    stack.faults.weights_oserror = False
    vectors = embedder.embed_images(_images(1))

    assert vectors.shape == (1, DIM)
    assert len(stack.open_clip.create_calls) == 2


def test_dimensao_diferente_da_registrada_e_rejeitada(tmp_path: Path) -> None:
    embedder = _embedder(FakeStack(), tmp_path, expected_dim=DIM + 1)

    with pytest.raises(EmbedderConfigError, match="dimensão"):
        embedder.embed_images(_images(1))


def test_dimensao_igual_a_registrada_e_aceita(tmp_path: Path) -> None:
    embedder = _embedder(FakeStack(), tmp_path, expected_dim=DIM)

    assert embedder.dim == DIM


def test_auto_usa_mps_quando_disponivel(tmp_path: Path) -> None:
    metrics = RecordingMetrics()
    stack = FakeStack(mps_available=True)
    embedder = _embedder(stack, tmp_path, metrics=metrics)

    embedder.embed_images(_images(2))

    assert embedder.device is Device.MPS
    assert stack.open_clip.create_calls[0].device == "mps"
    assert metrics.batches[0][0] is Device.MPS


def test_auto_usa_cpu_sem_mps(tmp_path: Path) -> None:
    stack = FakeStack(mps_available=False)

    embedder = _embedder(stack, tmp_path)

    assert embedder.device is Device.CPU


def test_cpu_explicito_ignora_mps_disponivel(tmp_path: Path) -> None:
    stack = FakeStack(mps_available=True)

    embedder = _embedder(stack, tmp_path, device="cpu")

    assert embedder.device is Device.CPU
    assert [call.device for call in stack.open_clip.create_calls] == ["cpu"]


def test_mps_pedido_sem_mps_disponivel_cai_para_cpu_com_aviso(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    embedder = _embedder(FakeStack(mps_available=False), tmp_path, device="mps")

    with caplog.at_level(logging.WARNING, logger="odc.features.embedder"):
        device = embedder.device

    assert device is Device.CPU
    assert "MPS indisponível" in caplog.text


def test_erro_de_mps_no_carregamento_cai_para_cpu(tmp_path: Path) -> None:
    stack = FakeStack(mps_available=True, faults=Faults(mps_load=True))
    embedder = _embedder(stack, tmp_path)

    vectors = embedder.embed_images(_images(2))

    assert vectors.shape == (2, DIM)
    assert embedder.device is Device.CPU
    assert [call.device for call in stack.open_clip.create_calls] == ["mps", "cpu"]


def test_erro_de_mps_na_inferencia_recarrega_em_cpu_e_continua(tmp_path: Path) -> None:
    metrics = RecordingMetrics()
    stack = FakeStack(mps_available=True, faults=Faults(mps_infer=True))
    embedder = _embedder(stack, tmp_path, metrics=metrics)
    images = _images(3)

    vectors = embedder.embed_images(images)
    again = embedder.embed_images(images)

    reference = _embedder(FakeStack(), tmp_path, device="cpu").embed_images(images)
    assert embedder.device is Device.CPU
    assert [call.device for call in stack.open_clip.create_calls] == ["mps", "cpu"]
    assert min(row_cosines(vectors, reference)) >= 0.999
    assert again.shape == vectors.shape
    assert {device for device, _, _ in metrics.batches} == {Device.CPU}


def test_erro_de_mps_na_inferencia_de_texto_recarrega_em_cpu(tmp_path: Path) -> None:
    stack = FakeStack(mps_available=True, faults=Faults(mps_infer=True))
    embedder = _embedder(stack, tmp_path)

    vectors = embedder.embed_texts(["a", "b"])

    assert vectors.shape == (2, DIM)
    assert embedder.device is Device.CPU


def test_erro_em_cpu_vira_embedding_error(tmp_path: Path) -> None:
    stack = FakeStack(faults=Faults(cpu_infer=True))
    embedder = _embedder(stack, tmp_path)

    with pytest.raises(EmbeddingError, match="inferência"):
        embedder.embed_images(_images(1))


def test_falha_tambem_no_fallback_de_cpu_vira_embedding_error(tmp_path: Path) -> None:
    stack = FakeStack(mps_available=True, faults=Faults(mps_infer=True, cpu_infer=True))
    embedder = _embedder(stack, tmp_path)

    with pytest.raises(EmbeddingError, match="CPU"):
        embedder.embed_images(_images(1))


def test_saida_com_formato_inesperado_vira_embedding_error(tmp_path: Path) -> None:
    embedder = _embedder(FakeStack(faults=Faults(bad_shape=True)), tmp_path)

    with pytest.raises(EmbeddingError, match="formato"):
        embedder.embed_images(_images(1))
