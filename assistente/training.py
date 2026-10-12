import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assistente.history import discover_project_events, history_database_path
from assistente.workspace import resolve_workspace_paths

CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
VSCODE_SESSION_DATABASE = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Code"
    / "User"
    / "globalStorage"
    / "github.copilot-chat"
    / "session-store.db"
)
TRAINING_MODEL = "mlx-community/Qwen2.5-3B-Instruct-4bit"
HUGGINGFACE_TRAINING_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
MAX_EXAMPLE_CHARS = 16_000
MAX_TRAINING_SEQUENCE = 1_024
MAX_TRACE_CHARS = 8_000
MAX_TRACE_EVENTS = 8
_SECRET_PATTERNS = (
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)"
        r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    ),
    re.compile(r"\b[A-Za-z0-9_-]{32,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END "
        r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"(?i)(https?://)[^/\s:@]+:[^@\s/]+@"),
    re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"),
    re.compile(r"(?<=/Users/)[^/\s]+"),
    re.compile(r"(?<=/home/)[^/\s]+"),
)


def sanitize_training_text(value: str) -> str:
    result = value.replace("\x00", " ").replace(str(Path.home()), "[USER_HOME]")
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result.strip()


def _text_from_blocks(blocks: Any, accepted_types: set[str]) -> str:
    if not isinstance(blocks, list):
        return ""
    parts = [
        item["text"]
        for item in blocks
        if isinstance(item, dict)
        and item.get("type") in accepted_types
        and isinstance(item.get("text"), str)
    ]
    return "\n".join(part.strip() for part in parts if part.strip())


def _sample(
    source: str,
    session_id: str,
    user_text: str,
    assistant_text: str,
) -> dict[str, Any] | None:
    user_text = sanitize_training_text(user_text)
    assistant_text = sanitize_training_text(assistant_text)
    if (
        not user_text
        or not assistant_text
        or len(user_text) + len(assistant_text) > MAX_EXAMPLE_CHARS
    ):
        return None
    identity = f"{source}:{session_id}"
    return {
        "identity": identity,
        "fingerprint": hashlib.sha256(
            f"{user_text.casefold()}\0{assistant_text.casefold()}".encode("utf-8")
        ).hexdigest(),
        "messages": [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ],
    }


def iter_codex_samples(session_dir: Path) -> Iterator[dict[str, Any]]:
    if not session_dir.is_dir():
        return
    for path in sorted(session_dir.rglob("*.jsonl")):
        pending_user: str | None = None
        with path.open(encoding="utf-8") as session_file:
            for line_number, line in enumerate(session_file, start=1):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"JSON inválido em {path.name}:{line_number}") from exc
                payload = record.get("payload")
                if record.get("type") != "response_item" or not isinstance(payload, dict):
                    continue
                if payload.get("type") != "message":
                    continue
                role = payload.get("role")
                if role == "user":
                    pending_user = _text_from_blocks(payload.get("content"), {"input_text"})
                elif role == "assistant" and payload.get("phase") == "final_answer":
                    assistant_text = _text_from_blocks(
                        payload.get("content"),
                        {"output_text"},
                    )
                    if pending_user is not None:
                        item = _sample("codex", path.stem, pending_user, assistant_text)
                        if item is not None:
                            yield item
                    pending_user = None


def iter_vscode_samples(database: Path) -> Iterator[dict[str, Any]]:
    if not database.is_file():
        return
    uri = f"{database.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute(
            """
            SELECT session_id, user_message, assistant_response
            FROM turns
            WHERE user_message IS NOT NULL AND assistant_response IS NOT NULL
            ORDER BY session_id, turn_index
            """
        )
        for session_id, user_text, assistant_text in rows:
            if isinstance(user_text, str) and isinstance(assistant_text, str):
                item = _sample("vscode", str(session_id), user_text, assistant_text)
                if item is not None:
                    yield item


def iter_imported_history_samples(database: Path) -> Iterator[dict[str, Any]]:
    if not database.is_file() or database.is_symlink():
        return
    uri = f"{database.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute(
            """
            SELECT source, session_id, user_message, assistant_response
            FROM conversation_turns
            WHERE user_message IS NOT NULL AND assistant_response IS NOT NULL
            ORDER BY source, session_id, turn_id
            """
        )
        for source, session_id, user_text, assistant_text in rows:
            if not all(
                isinstance(value, str) for value in (source, session_id, user_text, assistant_text)
            ):
                continue
            item = _sample(
                f"imported-{source}",
                session_id,
                user_text,
                assistant_text,
            )
            if item is not None:
                yield item


def _event_text(payload: Any, accepted_types: set[str]) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        return _text_from_blocks(payload, accepted_types)
    return ""


def _project_prompt(task: str, trace: list[str], instruction: str) -> str:
    history = "\n\n".join(trace) if trace else "(nenhum comando executado ainda)"
    return f"Tarefa: {task}\n\nHistórico de execução:\n{history}\n\n{instruction}"


def _append_trace(trace: list[str], entry: str) -> None:
    if len(entry) > MAX_TRACE_CHARS // 2:
        entry = (
            entry[: MAX_TRACE_CHARS // 4]
            + "\n[... saída truncada ...]\n"
            + entry[-MAX_TRACE_CHARS // 4 :]
        )
    trace.append(entry)
    while len(trace) > MAX_TRACE_EVENTS or sum(map(len, trace)) > MAX_TRACE_CHARS:
        trace.pop(0)


def iter_project_event_samples(
    project_name: str,
    *,
    home: Path | None = None,
    codex_db: Path | None = None,
) -> Iterator[dict[str, Any]]:
    current_key: tuple[str, str, str] | None = None
    task = ""
    trace: list[str] = []
    for event in discover_project_events(project_name, home=home, codex_db=codex_db):
        event_key = (event.source, event.session_id, event.turn_id)
        if event_key != current_key:
            current_key = event_key
            task = ""
            trace = []
        if event.event_type == "user_message":
            task = _event_text(event.payload, {"input_text", "text"})
        elif event.event_type == "tool_call" and task:
            arguments = json.dumps(event.payload, ensure_ascii=False, separators=(",", ":"))
            call = f"Ferramenta: {event.tool_name}\nArgumentos: {arguments}"
            item = _sample(
                f"project-{event.source}",
                event.session_id,
                _project_prompt(
                    task,
                    trace,
                    "Escolha a próxima ferramenta e seus argumentos.",
                ),
                call,
            )
            if item is not None:
                yield item
            _append_trace(trace, call)
        elif event.event_type == "tool_result" and task:
            output = json.dumps(event.payload, ensure_ascii=False, separators=(",", ":"))
            _append_trace(
                trace,
                f"Resultado da ferramenta {event.tool_name} (status: {event.status}):\n{output}",
            )
        elif event.event_type == "assistant_final" and task:
            answer = _event_text(event.payload, {"output_text", "text"})
            item = _sample(
                f"project-{event.source}",
                event.session_id,
                _project_prompt(
                    task,
                    trace,
                    "Responda ao usuário com base nos resultados observados.",
                ),
                answer,
            )
            if item is not None:
                yield item


def _data_dir() -> Path:
    return Path(
        os.environ.get(
            "CEREBRO_DATA_DIR",
            Path.home() / ".local" / "share" / "cerebro",
        )
    )


def load_long_term_learnings(database: Path | None = None) -> list[dict[str, str]]:
    runtime_database = database or resolve_workspace_paths().data_dir / "runtime.sqlite3"
    if not runtime_database.is_file() or runtime_database.is_symlink():
        return []
    uri = f"{runtime_database.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute(
            "SELECT topic, lesson FROM learnings ORDER BY created_at DESC, id DESC LIMIT 100"
        )
        learnings = []
        for topic, lesson in rows:
            if not isinstance(topic, str) or not isinstance(lesson, str):
                continue
            safe_topic = sanitize_training_text(topic)[:80]
            safe_lesson = sanitize_training_text(lesson)[:400]
            if safe_topic and safe_lesson and safe_lesson != "[REDACTED]":
                learnings.append({"topic": safe_topic, "lesson": safe_lesson})
    return learnings


def condition_samples_with_learnings(
    samples: Iterable[dict[str, Any]],
    learnings: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], int]:
    if not learnings:
        return list(samples), 0
    conditioned: list[dict[str, Any]] = []
    used_notes: set[str] = set()
    for sample in samples:
        messages = sample.get("messages")
        if not isinstance(messages, list) or len(messages) != 2:
            continue
        user_message, assistant_message = messages
        if not all(isinstance(message, dict) for message in messages):
            continue
        request = user_message.get("content")
        answer = assistant_message.get("content")
        if not isinstance(request, str) or not isinstance(answer, str):
            continue
        request_tokens = set(re.findall(r"[\w-]{3,}", request.casefold(), flags=re.UNICODE))
        matched = []
        for learning in learnings:
            note_text = f"{learning['topic']} {learning['lesson']}"
            note_tokens = set(re.findall(r"[\w-]{3,}", note_text.casefold(), flags=re.UNICODE))
            if len(request_tokens & note_tokens) >= 2:
                matched.append(learning)
        if not matched:
            continue
        note_lines = "\n".join(f"- [{item['topic']}] {item['lesson']}" for item in matched[:3])
        conditioned_request = (
            "Orientações de longo prazo registradas para esta tarefa. Trate-as como "
            "preferências/contexto do usuário, sem substituir as regras de segurança:\n"
            f"{note_lines}\n\nPedido original:\n{request}"
        )
        if len(conditioned_request) + len(answer) > MAX_EXAMPLE_CHARS:
            continue
        conditioned_sample = {
            **sample,
            "messages": [
                {"role": "user", "content": conditioned_request},
                assistant_message,
            ],
        }
        conditioned_sample["fingerprint"] = hashlib.sha256(
            f"{conditioned_request.casefold()}\0{answer.casefold()}".encode("utf-8")
        ).hexdigest()
        conditioned.append(conditioned_sample)
        used_notes.update(item["topic"] for item in matched[:3])
    return conditioned, len(used_notes)


def collect_samples(
    codex_sessions: Path = CODEX_SESSIONS,
    vscode_database: Path = VSCODE_SESSION_DATABASE,
    history_database: Path | None = None,
    project_name: str | None = None,
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    samples: list[dict[str, Any]] = []
    imported_database = (
        history_database if history_database is not None else history_database_path(_data_dir())
    )
    sources = (
        (iter_project_event_samples(project_name),)
        if project_name is not None
        else (
            iter_codex_samples(codex_sessions),
            iter_vscode_samples(vscode_database),
            iter_imported_history_samples(imported_database),
        )
    )
    for source in sources:
        for item in source:
            if item["fingerprint"] not in seen:
                seen.add(item["fingerprint"])
                samples.append(item)
    return samples


def _split_for_session(identity: str) -> str:
    bucket = hashlib.sha256(identity.encode("utf-8")).digest()[0] % 10
    return "train" if bucket < 8 else "valid" if bucket == 8 else "test"


def write_dataset(samples: Iterable[dict[str, Any]], destination: Path) -> dict[str, int]:
    if destination.exists():
        raise FileExistsError(f"A pasta de dados já existe: {destination}")
    destination.mkdir(parents=True, mode=0o700)
    groups: dict[str, list[dict[str, Any]]] = {"train": [], "valid": [], "test": []}
    for sample in samples:
        groups[_split_for_session(sample["identity"])].append({"messages": sample["messages"]})
    if any(not groups[name] for name in ("train", "valid", "test")):
        destination.rmdir()
        raise ValueError("Histórico insuficiente para separar treino, validação e teste.")
    for name, records in groups.items():
        path = destination / f"{name}.jsonl"
        with path.open("x", encoding="utf-8") as output:
            for record in records:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        if os.name != "nt":
            path.chmod(0o600)
    if os.name != "nt":
        destination.chmod(0o700)
    return {name: len(records) for name, records in groups.items()}


def _training_root() -> Path:
    configured_root = os.environ.get("CEREBRO_TRAINING_DIR")
    root = Path(configured_root).expanduser() if configured_root else _data_dir() / "training"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError("O diretório de treinamento não pode ser um link simbólico.")
    if os.name != "nt":
        root.chmod(0o700)
    return root


def prepare_dataset(
    root: Path | None = None,
    project_name: str | None = None,
    *,
    include_learnings: bool = False,
    runtime_database: Path | None = None,
) -> tuple[Path, dict[str, int]]:
    training_root = root or _training_root()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = training_root / "datasets" / timestamp
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    samples = collect_samples(project_name=project_name)
    if include_learnings:
        samples, notes_used = condition_samples_with_learnings(
            samples,
            load_long_term_learnings(runtime_database),
        )
        print(f"Notas de longo prazo aplicadas a exemplos reais: {notes_used}.")
    counts = write_dataset(
        samples,
        destination,
    )
    return destination, counts


def prepare_mlx_dataset(
    dataset: Path,
    tokenizer: Any,
    max_seq_length: int = MAX_TRAINING_SEQUENCE,
) -> tuple[Path, dict[str, int]]:
    destination = dataset / "mlx-ready"
    if destination.exists():
        raise FileExistsError(f"A pasta de dados MLX já existe: {destination}")
    destination.mkdir(mode=0o700)
    counts = {"train": 0, "valid": 0, "test": 0}
    skipped = 0

    for split in counts:
        with (dataset / f"{split}.jsonl").open(encoding="utf-8") as source:
            with (destination / f"{split}.jsonl").open("x", encoding="utf-8") as output:
                for line in source:
                    record = json.loads(line)
                    messages = record["messages"]
                    user_message, assistant_message = messages
                    original_prompt = user_message["content"]
                    prompt_tokens = tokenizer.encode(original_prompt, add_special_tokens=False)
                    full_tokens = tokenizer.apply_chat_template(
                        messages,
                        tokenize=True,
                        return_dict=False,
                    )
                    prefix_tokens = tokenizer.apply_chat_template(
                        messages[:1],
                        tokenize=True,
                        add_generation_prompt=True,
                        return_dict=False,
                    )
                    answer_tokens = len(full_tokens) - len(prefix_tokens)
                    prompt_overhead = max(0, len(prefix_tokens) - len(prompt_tokens))
                    if answer_tokens < 1 or answer_tokens > max_seq_length - 64:
                        skipped += 1
                        continue

                    prompt_budget = max_seq_length - answer_tokens - prompt_overhead - 16
                    if prompt_budget < 16:
                        skipped += 1
                        continue
                    if len(prompt_tokens) > prompt_budget:
                        user_message["content"] = tokenizer.decode(
                            prompt_tokens[-prompt_budget:],
                            skip_special_tokens=True,
                        )

                    full_tokens = tokenizer.apply_chat_template(
                        messages,
                        tokenize=True,
                        return_dict=False,
                    )
                    while len(full_tokens) > max_seq_length and prompt_budget > 16:
                        prompt_budget -= min(
                            prompt_budget - 16,
                            max(8, len(full_tokens) - max_seq_length + 8),
                        )
                        user_message["content"] = tokenizer.decode(
                            prompt_tokens[-prompt_budget:],
                            skip_special_tokens=True,
                        )
                        full_tokens = tokenizer.apply_chat_template(
                            messages,
                            tokenize=True,
                            return_dict=False,
                        )
                    if not user_message["content"] or len(full_tokens) > max_seq_length:
                        skipped += 1
                        continue

                    output.write(json.dumps(record, ensure_ascii=False) + "\n")
                    counts[split] += 1
        if os.name != "nt":
            (destination / f"{split}.jsonl").chmod(0o600)

    if any(count == 0 for count in counts.values()):
        for path in destination.iterdir():
            path.unlink()
        destination.rmdir()
        raise ValueError("Poucos exemplos cabem no contexto para treinar e avaliar.")
    if os.name != "nt":
        destination.chmod(0o700)
    print(f"Exemplos prontos para MLX: {counts}; ignorados por limite de contexto: {skipped}")
    return destination, counts


def train(dataset: Path, adapter: Path, model: str = TRAINING_MODEL) -> int:
    executable = Path(sys.prefix) / "bin" / "mlx_lm.lora"
    if not executable.is_file():
        raise FileNotFoundError(
            "MLX-LM não está instalado no ambiente ativo. Instale com "
            "`python3.13 -m pip install mlx-lm`."
        )
    if adapter.exists():
        raise FileExistsError(f"O adaptador já existe: {adapter}")
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("O ambiente de treinamento precisa incluir transformers.") from exc
    tokenizer = AutoTokenizer.from_pretrained(model)
    mlx_dataset, counts = prepare_mlx_dataset(dataset, tokenizer)
    with (mlx_dataset / "train.jsonl").open(encoding="utf-8") as training_file:
        iterations = sum(1 for _ in training_file)
    if iterations < 1:
        raise ValueError("O conjunto de treino está vazio.")
    adapter.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    adapter.mkdir(mode=0o700)
    command = [
        str(executable),
        "--model",
        model,
        "--data",
        str(mlx_dataset),
        "--adapter-path",
        str(adapter),
        "--fine-tune-type",
        "lora",
        "--mask-prompt",
        "--batch-size",
        "1",
        "--num-layers",
        "8",
        "--max-seq-length",
        "1024",
        "--iters",
        str(iterations),
        "--train",
        "--test",
    ]
    return subprocess.run(command, check=False).returncode


def _load_huggingface_split(dataset: Path, split: str, dataset_class: Any) -> Any:
    records: list[dict[str, Any]] = []
    with (dataset / f"{split}.jsonl").open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"JSON inválido em {split}.jsonl:{line_number}"
                ) from exc
            messages = record.get("messages") if isinstance(record, dict) else None
            if (
                not isinstance(messages, list)
                or len(messages) != 2
                or not all(isinstance(message, dict) for message in messages)
                or messages[0].get("role") != "user"
                or messages[1].get("role") != "assistant"
                or not all(
                    isinstance(message.get("content"), str) and message["content"].strip()
                    for message in messages
                )
            ):
                raise ValueError(
                    f"Exemplo inválido em {split}.jsonl:{line_number}; "
                    "esperados exatamente um pedido e uma resposta."
                )
            records.append(
                {
                    "prompt": [{"role": "user", "content": messages[0]["content"]}],
                    "completion": [
                        {"role": "assistant", "content": messages[1]["content"]}
                    ],
                }
            )
    if not records:
        raise ValueError(f"O conjunto {split} está vazio.")
    return dataset_class.from_list(records)


def train_huggingface(
    dataset: Path,
    adapter: Path,
    model: str = HUGGINGFACE_TRAINING_MODEL,
) -> int:
    if adapter.exists():
        raise FileExistsError(f"O adaptador já existe: {adapter}")
    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig
        from trl import SFTConfig, SFTTrainer
    except ImportError as exc:
        raise RuntimeError(
            "O backend Hugging Face requer as dependências de "
            "`requirements-training-hf.txt` no ambiente de treinamento."
        ) from exc

    train_data = _load_huggingface_split(dataset, "train", Dataset)
    validation_data = _load_huggingface_split(dataset, "valid", Dataset)
    test_data = _load_huggingface_split(dataset, "test", Dataset)
    cuda_available = torch.cuda.is_available()
    mps_available = torch.backends.mps.is_available()
    if cuda_available and torch.cuda.is_bf16_supported():
        model_dtype = torch.bfloat16
    elif cuda_available or mps_available:
        model_dtype = torch.float16
    else:
        model_dtype = torch.float32

    adapter.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    training_args = SFTConfig(
        output_dir=str(adapter / "checkpoints"),
        num_train_epochs=1,
        per_device_train_batch_size=1,
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=4,
        learning_rate=2e-4,
        logging_steps=1,
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        max_length=MAX_TRAINING_SEQUENCE,
        completion_only_loss=True,
        report_to="none",
        model_init_kwargs={"torch_dtype": model_dtype},
    )
    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=train_data,
        eval_dataset=validation_data,
        peft_config=LoraConfig(
            r=8,
            lora_alpha=16,
            lora_dropout=0.05,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=[
                "q_proj",
                "k_proj",
                "v_proj",
                "o_proj",
                "gate_proj",
                "up_proj",
                "down_proj",
            ],
        ),
    )
    trainer.train()
    trainer.evaluate(eval_dataset=test_data, metric_key_prefix="test")
    trainer.model.save_pretrained(adapter)
    trainer.processing_class.save_pretrained(adapter)
    if os.name != "nt":
        for path in adapter.rglob("*"):
            if path.is_dir():
                path.chmod(0o700)
            elif path.is_file():
                path.chmod(0o600)
        adapter.chmod(0o700)
    return 0


def _validated_prepared_dataset(dataset: Path) -> Path:
    training_root = _training_root() / "datasets"
    expanded_dataset = dataset.expanduser()
    if expanded_dataset.is_symlink():
        raise ValueError("O dataset de treinamento não pode ser um link simbólico.")
    resolved_dataset = expanded_dataset.resolve(strict=True)
    resolved_root = training_root.resolve()
    if not resolved_dataset.is_dir() or not resolved_dataset.is_relative_to(resolved_root):
        raise ValueError(
            "Use o caminho de um dataset preparado pelo comando `prepare`, "
            f"dentro de {resolved_root}."
        )
    for split in ("train", "valid", "test"):
        split_file = resolved_dataset / f"{split}.jsonl"
        if split_file.is_symlink() or not split_file.is_file():
            raise ValueError(f"Arquivo obrigatório ausente ou inválido: {split_file}")
    return resolved_dataset


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fine-tuning local LoRA usando históricos locais do VS Code e Codex."
    )
    parser.add_argument(
        "command",
        choices=("prepare", "train", "train-hf"),
        help=(
            "prepare cria dados sanitizados; train usa MLX; "
            "train-hf usa Transformers, TRL e PEFT localmente."
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Modelo base; o padrão depende do backend escolhido.",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        help="Dataset sanitizado criado antes com `prepare` (obrigatório em train-hf).",
    )
    parser.add_argument(
        "--project-name",
        help="Usa somente rollouts do Codex cujo workspace termine com esse nome exato.",
    )
    parser.add_argument(
        "--include-learnings",
        action="store_true",
        help=(
            "Inclui notas relevantes de longo prazo como contexto em pares reais "
            "de histórico; notas sem exemplos correspondentes não viram respostas sintéticas."
        ),
    )
    args = parser.parse_args()

    try:
        if args.command == "train-hf":
            if args.dataset is None:
                parser.error(
                    "train-hf exige --dataset com o caminho de um dataset preparado "
                    "e revisado."
                )
            if args.project_name or args.include_learnings:
                parser.error(
                    "--project-name e --include-learnings devem ser usados em `prepare` "
                    "antes de `train-hf`."
                )
            dataset = _validated_prepared_dataset(args.dataset)
            counts = {}
            for split in ("train", "valid", "test"):
                with (dataset / f"{split}.jsonl").open(encoding="utf-8") as source:
                    counts[split] = sum(1 for _ in source)
        else:
            if args.dataset is not None:
                parser.error("--dataset só pode ser usado com train-hf.")
            dataset, counts = prepare_dataset(
                project_name=args.project_name,
                include_learnings=args.include_learnings,
            )
        print("Dados preparados: " + ", ".join(f"{name}={count}" for name, count in counts.items()))
        if args.command == "prepare":
            print(f"Arquivos privados em: {dataset}")
            return 0
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if args.command == "train-hf":
            model = args.model or HUGGINGFACE_TRAINING_MODEL
            adapter = _training_root() / "adapters" / f"qwen2.5-hf-{timestamp}"
            result = train_huggingface(dataset, adapter, model)
        else:
            model = args.model or TRAINING_MODEL
            adapter = _training_root() / "adapters" / f"qwen2.5-3b-{timestamp}"
            result = train(dataset, adapter, model)
        if result == 0:
            print(f"Adaptador LoRA treinado: {adapter}")
        return result
    except (
        FileNotFoundError,
        FileExistsError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        parser.exit(1, f"Erro no treinamento local: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
