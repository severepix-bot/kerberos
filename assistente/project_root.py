import os
from pathlib import Path
from typing import Mapping

_PROJECT_MARKERS = (
    ".git",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "composer.json",
    "Gemfile",
    "requirements.txt",
    "README.md",
)


def resolve_project_root(
    default_root: Path,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    configured_root = env.get("CEREBRO_PROJECT_ROOT")
    if configured_root is None:
        root = default_root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(f"A pasta padrão do projeto não é um diretório: {root}")
        return root

    configured_root = configured_root.strip()
    if not configured_root:
        raise ValueError("CEREBRO_PROJECT_ROOT não pode ficar vazio.")

    candidate = Path(configured_root).expanduser()
    if not candidate.is_absolute():
        raise ValueError("CEREBRO_PROJECT_ROOT deve ser um caminho absoluto.")
    try:
        root = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            f"CEREBRO_PROJECT_ROOT não aponta para um diretório acessível: {candidate}"
        ) from exc
    if not root.is_dir():
        raise ValueError(f"CEREBRO_PROJECT_ROOT não é um diretório: {root}")

    user_home = (Path.home() if home is None else home).expanduser().resolve()
    if root == Path(root.anchor) or root == user_home:
        raise ValueError("CEREBRO_PROJECT_ROOT deve apontar para uma pasta de projeto específica.")
    if not any((root / marker).exists() for marker in _PROJECT_MARKERS):
        raise ValueError(
            "CEREBRO_PROJECT_ROOT não parece ser uma pasta de projeto; "
            "nenhum marcador conhecido foi encontrado."
        )
    return root
