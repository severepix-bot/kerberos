import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_WORKSPACE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")


@dataclass(frozen=True)
class WorkspacePaths:
    name: str | None
    root: Path | None
    data_dir: Path
    mcp_config: Path
    hubs_config: Path


def resolve_workspace_paths(
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> WorkspacePaths:
    env = os.environ if environ is None else environ
    user_home = Path.home() if home is None else home
    name = env.get("CEREBRO_WORKSPACE")
    default_data = user_home / ".local" / "share" / "cerebro"
    default_mcp = user_home / ".config" / "assistente-local" / "mcp.json"
    default_hubs = user_home / ".config" / "cerebro" / "hubs.json"

    if name is None:
        return WorkspacePaths(
            name=None,
            root=None,
            data_dir=Path(env.get("CEREBRO_DATA_DIR", default_data)),
            mcp_config=Path(env.get("ASSISTENTE_MCP_CONFIG", default_mcp)),
            hubs_config=Path(env.get("CEREBRO_HUBS_CONFIG", default_hubs)),
        )

    if not _WORKSPACE_NAME.fullmatch(name):
        raise ValueError(
            "CEREBRO_WORKSPACE deve conter de 1 a 40 caracteres: "
            "letras minúsculas, números e hífens internos."
        )

    data_base = Path(env.get("CEREBRO_DATA_DIR", default_data))
    mcp_base = Path(env.get("ASSISTENTE_MCP_CONFIG", default_mcp)).parent
    hubs_base = Path(env.get("CEREBRO_HUBS_CONFIG", default_hubs)).parent
    root = data_base / "workspaces" / name
    return WorkspacePaths(
        name=name,
        root=root,
        data_dir=root / "data",
        mcp_config=mcp_base / "workspaces" / name / "mcp.json",
        hubs_config=hubs_base / "workspaces" / name / "hubs.json",
    )


def prepare_workspace(paths: WorkspacePaths) -> None:
    if paths.root is None or paths.name is None:
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        return

    data_base = paths.root.parent.parent
    config_dir = paths.mcp_config.parent
    hubs_dir = paths.hubs_config.parent
    for base, profile_dir in (
        (data_base, paths.root),
        (config_dir.parent.parent, config_dir),
        (hubs_dir.parent.parent, hubs_dir),
    ):
        workspace_parent = base / "workspaces"
        if (
            workspace_parent.is_symlink()
            or profile_dir.is_symlink()
            or (profile_dir == paths.root and paths.data_dir.is_symlink())
        ):
            raise ValueError("Os diretórios de workspace não podem ser links simbólicos.")
    paths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    hubs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for base, profile_dir in (
        (data_base, paths.root),
        (config_dir.parent.parent, config_dir),
        (hubs_dir.parent.parent, hubs_dir),
    ):
        expected_dir = base.resolve() / "workspaces" / paths.name
        if profile_dir.resolve() != expected_dir:
            raise ValueError("O caminho resolvido do workspace saiu do diretório isolado.")
    if paths.data_dir.resolve() != paths.root.resolve() / "data":
        raise ValueError("O caminho de dados do workspace saiu do diretório isolado.")
    if os.name != "nt":
        for directory in {paths.root, paths.data_dir, config_dir, hubs_dir}:
            directory.chmod(0o700)


WORKSPACE_PATHS = resolve_workspace_paths()
