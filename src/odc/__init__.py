# SPDX-License-Identifier: GPL-3.0-only
"""One Drive Cleaner: a versão em runtime vem dos metadados da distribuição instalada."""

from importlib import metadata

__version__ = metadata.version("onedrive-cleaner")
