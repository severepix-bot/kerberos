import os
import queue
import secrets
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_PROCESSES = 8
MAX_RETAINED_PROCESSES = 64
MAX_OUTPUT_BYTES = 64 * 1024
MAX_INPUT_BYTES = 8 * 1024
MAX_QUEUED_INPUTS = 16


@dataclass
class ManagedProcess:
    process: subprocess.Popen[bytes]
    command: list[str]
    cwd: Path
    output: bytearray = field(default_factory=bytearray)
    output_lock: threading.Lock = field(default_factory=threading.Lock)
    input_queue: queue.Queue[bytes] = field(
        default_factory=lambda: queue.Queue(maxsize=MAX_QUEUED_INPUTS)
    )
    output_cursor: int = 0
    output_start: int = 0


class ProjectProcessManager:
    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).resolve()
        self._processes: dict[str, ManagedProcess] = {}
        self._lock = threading.Lock()
        self._closed = False

    def start(self, command: Any, cwd: str = ".") -> str:
        if (
            not isinstance(command, list)
            or not command
            or len(command) > 64
            or any(
                not isinstance(item, str) or not item or "\x00" in item or len(item) > 8000
                for item in command
            )
        ):
            raise ValueError("Comando inválido; informe argumentos argv não vazios.")
        if cwd != "." and (
            not isinstance(cwd, str) or not cwd or "\x00" in cwd or Path(cwd).is_absolute()
        ):
            raise ValueError("cwd deve ser um caminho relativo à raiz do projeto.")

        working_directory = (self.project_root / cwd).resolve()
        try:
            working_directory.relative_to(self.project_root)
        except ValueError as exc:
            raise ValueError("O diretório do processo deve ficar dentro do projeto.") from exc
        if not working_directory.is_dir():
            raise NotADirectoryError(f"Diretório de trabalho não encontrado: {cwd}")

        with self._lock:
            if self._closed:
                raise RuntimeError("O gerenciador de processos está encerrado.")
            active = sum(item.process.poll() is None for item in self._processes.values())
            if active >= MAX_PROCESSES:
                raise RuntimeError(f"Limite de {MAX_PROCESSES} processos simultâneos atingido.")
            if len(self._processes) >= MAX_RETAINED_PROCESSES:
                for old_id, old_process in list(self._processes.items()):
                    if old_process.process.poll() is not None:
                        del self._processes[old_id]
                        break
                else:
                    raise RuntimeError(
                        f"Limite de {MAX_RETAINED_PROCESSES} sessões de processo retidas atingido."
                    )

            options: dict[str, Any] = {
                "args": command,
                "cwd": working_directory,
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "bufsize": 0,
                "shell": False,
            }
            if os.name == "nt":
                options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                options["start_new_session"] = True
            process = subprocess.Popen(**options)

            process_id = secrets.token_hex(16)
            managed = ManagedProcess(process, list(command), working_directory)
            self._processes[process_id] = managed
            threading.Thread(
                target=self._read_output,
                args=(managed,),
                daemon=True,
                name=f"cerebro-process-output-{process_id[:8]}",
            ).start()
            threading.Thread(
                target=self._write_input,
                args=(managed,),
                daemon=True,
                name=f"cerebro-process-input-{process_id[:8]}",
            ).start()
        return (
            f"Processo iniciado: {process_id}\n"
            f"Executável: {Path(command[0]).name}\n"
            f"Diretório: {working_directory.relative_to(self.project_root) or '.'}"
        )

    @staticmethod
    def _read_output(managed: ManagedProcess) -> None:
        stream = managed.process.stdout
        if stream is None:
            return
        try:
            while chunk := stream.read(4096):
                with managed.output_lock:
                    managed.output.extend(chunk)
                    overflow = len(managed.output) - MAX_OUTPUT_BYTES
                    if overflow > 0:
                        del managed.output[:overflow]
                        managed.output_start += overflow
        except OSError:
            return
        finally:
            stream.close()

    @staticmethod
    def _write_input(managed: ManagedProcess) -> None:
        stream = managed.process.stdin
        if stream is None:
            return
        while True:
            try:
                data = managed.input_queue.get(timeout=0.25)
            except queue.Empty:
                if managed.process.poll() is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
                    return
                continue
            try:
                stream.write(data)
                stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                return
            finally:
                managed.input_queue.task_done()

    def list_processes(self) -> list[dict[str, str | int | None]]:
        with self._lock:
            processes = list(self._processes.items())
        return [
            {
                "process_id": process_id,
                "executable": Path(managed.command[0]).name,
                "cwd": str(managed.cwd.relative_to(self.project_root) or "."),
                "status": "running" if managed.process.poll() is None else "finished",
                "exit_code": managed.process.poll(),
            }
            for process_id, managed in processes
        ]

    def read_output(self, process_id: str) -> str:
        managed = self._get(process_id)
        with managed.output_lock:
            dropped = max(0, managed.output_start - managed.output_cursor)
            start = max(0, managed.output_cursor - managed.output_start)
            output = bytes(managed.output[start:])
            managed.output_cursor = managed.output_start + len(managed.output)
        status = (
            "running"
            if managed.process.poll() is None
            else f"finished (exit code {managed.process.returncode})"
        )
        prefix = f"Processo {process_id}: {status}\n"
        if dropped:
            prefix += f"[{dropped} bytes antigos descartados; buffer limitado]\n"
        return prefix + output.decode("utf-8", errors="replace")

    def send_input(self, process_id: str, text: str) -> str:
        if not isinstance(text, str) or "\x00" in text:
            raise ValueError("A entrada deve ser texto sem bytes NUL.")
        data = (text.rstrip("\r\n") + "\n").encode("utf-8")
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError(f"A entrada excede o limite de {MAX_INPUT_BYTES} bytes.")
        managed = self._get(process_id)
        if managed.process.poll() is not None:
            raise RuntimeError("O processo já terminou e não aceita entrada.")
        try:
            managed.input_queue.put_nowait(data)
        except queue.Full as exc:
            raise RuntimeError("A fila de entrada está cheia; aguarde e tente novamente.") from exc
        return f"Entrada enfileirada para o processo {process_id}."

    def stop(self, process_id: str) -> str:
        managed = self._get(process_id)
        process = managed.process
        if process.poll() is None:
            try:
                if os.name == "nt":
                    process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                self._force_stop(process)
        return f"Processo {process_id} encerrado (exit code {process.poll()})."

    @staticmethod
    def _force_stop(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
        process.wait()

    def _get(self, process_id: str) -> ManagedProcess:
        if not isinstance(process_id, str) or len(process_id) != 32:
            raise ValueError("ID de processo inválido.")
        with self._lock:
            managed = self._processes.get(process_id)
        if managed is None:
            raise KeyError("Processo não encontrado nesta sessão do Cerebro.")
        return managed

    def close(self) -> None:
        with self._lock:
            self._closed = True
            process_ids = list(self._processes)
        for process_id in process_ids:
            self.stop(process_id)
