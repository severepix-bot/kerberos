import argparse

from assistente.history import (
    ConversationHistoryStore,
    discover_history_turns,
    discover_project_events,
    history_database_path,
)
from assistente.workspace import WORKSPACE_PATHS


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Importa conversas locais do VS Code e Codex para referência do Kerberos."
    )
    parser.add_argument(
        "--data-dir",
        help="Pasta de dados do Kerberos (padrão: CEREBRO_DATA_DIR ou ~/.local/share/cerebro).",
    )
    parser.add_argument(
        "--project-name",
        help=(
            "Importa também pedidos, respostas finais, chamadas de ferramenta e resultados "
            "dos rollouts locais cujo diretório do projeto tenha exatamente esse nome."
        ),
    )
    args = parser.parse_args()
    path = history_database_path(args.data_dir or WORKSPACE_PATHS.data_dir)
    store = ConversationHistoryStore(path)
    imported_turns = store.import_turns(discover_history_turns())
    print(f"Turnos importados/atualizados: {imported_turns}; total local: {store.count()}.")
    if args.project_name:
        imported_events = store.import_events(discover_project_events(args.project_name))
        print(f"Eventos do projeto importados/atualizados: {imported_events}.")
    print(f"Banco local: {path}")


if __name__ == "__main__":
    main()
