# Agente Python com LangChain e Ollama

Este exemplo conecta um modelo local do Ollama a ferramentas explícitas de gravação de arquivo e verificações do projeto. Os caminhos ficam limitados à pasta configurada, cada ação pede confirmação no terminal e os comandos são executados como argumentos separados, sem shell.

## 1. Instalar dependências e preparar o modelo

Em um ambiente virtual Python:

```bash
python -m pip install "langchain>=1,<2" langchain-ollama langchain-community
ollama pull qwen2.5:7b
```

O exemplo usa a API atual `create_agent` do pacote `langchain`. `langchain-community` fica disponível para integrações adicionais, mas não é necessário para as ferramentas locais abaixo. O Ollama deve estar instalado e em execução.

Para modelos com suporte a tool calling, considere `qwen2.5:7b`, `llama3.1` ou `mistral`. A disponibilidade, qualidade e memória necessária variam conforme a variante e o hardware. Confira os modelos instalados com `ollama list`.

## 2. Criar e executar o agente

Salve como `agente_ollama.py` na pasta do projeto:

```python
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_ollama import ChatOllama

PROJECT_ROOT = Path(
    os.environ.get("CEREBRO_PROJECT_ROOT", Path.cwd())
).expanduser().resolve()
MAX_FILE_BYTES = 200_000
MAX_OUTPUT_CHARS = 20_000
ALLOWED_CHECKS = {
    ("git", "status", "--short"),
    ("pytest", "-q"),
}


def confirm(action: str) -> bool:
    answer = input(f"{action}\nDigite 's' para confirmar: ").strip().casefold()
    return answer in {"s", "sim"}


@tool
def criar_arquivo_texto(nome: str, conteudo: str) -> str:
    """Cria ou atualiza um arquivo de texto dentro da pasta do projeto, após confirmação."""
    relative_path = Path(nome)
    if (
        not nome.strip()
        or relative_path.is_absolute()
        or ".." in relative_path.parts
        or "\x00" in nome
    ):
        return "Erro: informe um caminho relativo dentro do projeto."
    if len(conteudo.encode("utf-8")) > MAX_FILE_BYTES:
        return f"Erro: o arquivo excede o limite de {MAX_FILE_BYTES} bytes."

    target = PROJECT_ROOT / relative_path
    cursor = PROJECT_ROOT
    for part in relative_path.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return "Erro: não é permitido acessar links simbólicos."
    target = target.resolve()
    if not target.is_relative_to(PROJECT_ROOT):
        return "Erro: o caminho precisa permanecer dentro do projeto."
    if not target.parent.is_dir():
        return "Erro: o diretório de destino precisa existir dentro do projeto."
    if target.exists() and not target.is_file():
        return "Erro: o destino existente não é um arquivo regular."
    action = f"Criar/atualizar {target.relative_to(PROJECT_ROOT)} ({len(conteudo)} caracteres)"
    if not confirm(action):
        return "Ação cancelada."

    try:
        target.write_text(conteudo, encoding="utf-8")
    except OSError as error:
        return f"Erro ao salvar arquivo: {error}"
    return f"Arquivo salvo: {target.relative_to(PROJECT_ROOT)}"


@tool
def executar_verificacao(comando: str) -> str:
    """Executa apenas git status --short ou pytest -q, após mostrar e confirmar o comando."""
    try:
        arguments = tuple(shlex.split(comando))
    except ValueError as error:
        return f"Comando inválido: {error}"

    if arguments not in ALLOWED_CHECKS:
        return "Comando não permitido. Opções: git status --short ou pytest -q."
    if not confirm(f"Executar no projeto {PROJECT_ROOT}: {' '.join(arguments)}"):
        return "Execução cancelada."

    executable = arguments[0]
    resolved_executable = (
        shutil.which(executable) if executable == "git" else sys.executable
    )
    if resolved_executable is None:
        return f"Executável não encontrado: {executable}"
    argv = (
        [resolved_executable, *arguments[1:]]
        if executable == "git"
        else [resolved_executable, "-m", "pytest", "-q"]
    )
    try:
        result = subprocess.run(
            argv,
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=120,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return "A verificação excedeu o limite de 120 segundos e foi encerrada."
    except OSError as error:
        return f"Não foi possível iniciar a verificação: {error}"
    output = result.stdout[-MAX_OUTPUT_CHARS:]
    return f"Exit code: {result.returncode}\n{output}".rstrip()


tools = [criar_arquivo_texto, executar_verificacao]
llm = ChatOllama(model="qwen2.5:7b", temperature=0)
agent = create_agent(
    model=llm,
    tools=tools,
    system_prompt=(
        "Você é um agente local de desenvolvimento. Use as ferramentas quando solicitadas. "
        "Explique o que pretende alterar ou verificar; a ferramenta pede confirmação antes "
        "de cada operação. Não afirme que uma ação foi concluída se a ferramenta não confirmar."
    ),
)

if __name__ == "__main__":
    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Crie relatorio.txt com o texto "
                        "'Sistema atualizado com sucesso'."
                    ),
                }
            ]
        }
    )
    print(result["messages"][-1].content)
```

Para escolher outra raiz, inicie o processo com `CEREBRO_PROJECT_ROOT` apontando para uma pasta de projeto existente. Esta restrição de caminho para arquivos não transforma os processos do sistema em sandbox.

`ALLOWED_CHECKS` é uma lista fechada de verificações, não uma interface para Bash. Antes de permitir um comando novo, examine o seu efeito e adicione uma forma exata e validada à lista; mantenha a aprovação e `shell=False`. Para iniciar servidores ou outros processos de longa duração, use um gerenciador de processos com IDs e encerramento explícito, em vez de deixar um `subprocess.Popen` sem supervisão.

## 3. Interfaces visuais

- **Open WebUI:** interface para Ollama que pode ser ampliada com ferramentas e pipelines; examine as permissões e o código de cada extensão antes de habilitá-la.
- **Flowise / Langflow:** editores visuais para compor fluxos e conectar modelos a ferramentas e serviços.
- **Fabric:** interface de linha de comando para aplicar padrões de prompts a tarefas com modelos locais.

Esses produtos e extensões podem executar ações com as permissões do usuário que os iniciou. Use projetos de teste, conceda apenas acesso necessário e não conecte ferramentas de execução arbitrária sem controles de aprovação.

## 4. Alternativa: API REST do Ollama com várias chamadas de ferramenta

LangChain cuida do ciclo de ferramentas pelo agente. Para entender ou controlar esse ciclo diretamente, instale `requests`:

```bash
python -m pip install requests
```

O exemplo abaixo usa duas ferramentas locais somente de leitura para demonstrar uma tarefa em várias etapas. Substitua-as por funções do seu aplicativo, mantendo uma lista explícita de ferramentas permitidas. O loop tem limite de iterações, valida o nome da ferramenta e devolve erros de argumentos ao modelo; ele nunca executa código ou comandos recebidos do modelo.

```python
import requests

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:7b"
MAX_ITERATIONS = 6


def buscar_perfil(nome: str) -> dict:
    """Busca um perfil em uma fonte de dados de demonstração."""
    perfis = {
        "ana": {"cidade": "São Paulo", "plano": "Premium"},
        "carlos": {"cidade": "Curitiba", "plano": "Basic"},
    }
    perfil = perfis.get(nome.casefold())
    if perfil is None:
        raise ValueError("Perfil não encontrado.")
    return perfil


def obter_clima(cidade: str) -> str:
    """Retorna uma resposta simulada; conecte aqui um serviço meteorológico."""
    climas = {"São Paulo": "22°C, chuva leve", "Curitiba": "15°C, nublado"}
    return climas.get(cidade, "Condição indisponível.")


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "buscar_perfil",
            "description": "Busca a cidade de uma pessoa pelo nome.",
            "parameters": {
                "type": "object",
                "properties": {"nome": {"type": "string"}},
                "required": ["nome"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "obter_clima",
            "description": "Consulta a condição meteorológica de uma cidade.",
            "parameters": {
                "type": "object",
                "properties": {"cidade": {"type": "string"}},
                "required": ["cidade"],
            },
        },
    },
]
HANDLERS = {"buscar_perfil": buscar_perfil, "obter_clima": obter_clima}


def executar(pergunta: str) -> str:
    messages = [{"role": "user", "content": pergunta}]

    for _ in range(MAX_ITERATIONS):
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "messages": messages,
                "tools": TOOLS,
                "stream": False,
            },
            timeout=120,
        )
        response.raise_for_status()
        message = response.json().get("message")
        if not isinstance(message, dict):
            raise RuntimeError("Resposta inválida da API do Ollama.")

        messages.append(message)
        tool_calls = message.get("tool_calls", [])
        if not tool_calls:
            return message.get("content", "")

        for call in tool_calls:
            function = call.get("function", {})
            name = function.get("name")
            arguments = function.get("arguments", {})
            handler = HANDLERS.get(name)

            if handler is None:
                result = f"Erro: ferramenta não permitida: {name!r}."
            elif not isinstance(arguments, dict):
                result = "Erro: os argumentos da ferramenta devem ser um objeto."
            else:
                try:
                    result = handler(**arguments)
                except (TypeError, ValueError) as error:
                    result = f"Erro nos argumentos ou na ferramenta: {error}"

            messages.append(
                {
                    "role": "tool",
                    "tool_name": str(name),
                    "content": str(result),
                }
            )

    raise RuntimeError(
        f"O agente atingiu o limite de {MAX_ITERATIONS} iterações sem concluir."
    )


if __name__ == "__main__":
    print(executar("A Ana vai viajar. Qual é o clima na cidade dela?"))
```

Cada resposta do modelo é adicionada ao histórico. Quando há chamadas, o programa executa apenas funções do dicionário `HANDLERS`, envia os resultados como mensagens `tool` e volta a consultar o Ollama. Sem chamadas de ferramenta, retorna a resposta final. Consulte a [documentação de tool calling do Ollama](https://docs.ollama.com/capabilities/tool-calling) para detalhes sobre schemas e chamadas paralelas.
