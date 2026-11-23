# SPDX-License-Identifier: GPL-3.0-only
"""Pesos reais do modelo aprovado (ViT-B-32/laion2b_s34b_b79k) para os testes `model`.

Os pesos vêm do cache local do Hugging Face (somente leitura, via symlink para um diretório
temporário); sem cache utilizável (ausente ou apenas o esqueleto de pastas, sem o arquivo de
pesos), o teste é pulado, a menos que `ODC_MODEL_TEST_DOWNLOAD=1` autorize baixar
~600 MB para o diretório temporário. Nunca usa `odc.paths` nem diretórios reais da aplicação.
"""

import os
from pathlib import Path
from typing import Final

import pytest

from odc.features.embedder import EmbedderOptions, OpenClipEmbedder

MODEL: Final = "ViT-B-32"
PRETRAINED: Final = "laion2b_s34b_b79k"
MODEL_NAME: Final = f"{MODEL}/{PRETRAINED}"
DIM: Final = 512
HF_ENTRY: Final = "models--laion--CLIP-ViT-B-32-laion2B-s34B-b79K"
WEIGHTS_FILE: Final = "open_clip_model.safetensors"


def hf_hub_cache() -> Path:
    """Diretório do cache do Hugging Face (`HF_HUB_CACHE` > `HF_HOME/hub` > `~/.cache`)."""
    hf_home = os.environ.get("HF_HOME")
    base = Path(hf_home) if hf_home else Path.home() / ".cache" / "huggingface"
    return Path(os.environ.get("HF_HUB_CACHE", base / "hub"))


def has_weights(entry: Path) -> bool:
    """Há um snapshot com o arquivo de pesos resolvido (symlink quebrado não conta)."""
    return any(path.is_file() for path in entry.glob(f"snapshots/*/{WEIGHTS_FILE}"))


def prepare_weights_dir(target: Path) -> Path:
    """Aponta `target` para o cache local; pula o teste se não houver pesos nem autorização."""
    pytest.importorskip("open_clip")
    source = hf_hub_cache() / HF_ENTRY
    if has_weights(source):
        (target / HF_ENTRY).symlink_to(source, target_is_directory=True)
    elif os.environ.get("ODC_MODEL_TEST_DOWNLOAD") != "1":
        pytest.skip("pesos ausentes do cache do Hugging Face (ODC_MODEL_TEST_DOWNLOAD=1 baixa)")
    return target


def make_embedder(weights_dir: Path, device: str) -> OpenClipEmbedder:
    """Embedder real lendo os pesos de `weights_dir`."""
    options = EmbedderOptions(expected_dim=DIM, models_dir=lambda: weights_dir)
    return OpenClipEmbedder(MODEL, PRETRAINED, device, options=options)
