import re
import shutil

_SSH_HOST_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def remote_terminal_command(provider: str, target: str = "") -> list[str]:
    if provider == "ssh-vm":
        if not isinstance(target, str) or not _SSH_HOST_ALIAS.fullmatch(target):
            raise ValueError("Use um alias de host SSH válido, configurado em ~/.ssh/config.")
        executable = shutil.which("ssh")
        if executable is None:
            raise FileNotFoundError("OpenSSH (ssh) não está instalado.")
        return [executable, "-tt", target]

    if provider == "google-cloud-shell":
        if target:
            raise ValueError("O Cloud Shell do Google usa a conta/projeto ativo do gcloud.")
        executable = shutil.which("gcloud")
        if executable is None:
            raise FileNotFoundError("Google Cloud CLI (gcloud) não está instalado.")
        return [executable, "cloud-shell", "ssh", "--ssh-flag=-tt"]

    raise ValueError("Provedor remoto não suportado.")
