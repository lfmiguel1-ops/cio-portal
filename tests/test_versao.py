"""Prioridade 2: a versão de implantação é uma só em código, imagem, compose e guias."""
import re
from pathlib import Path

from cio import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_versao_unica_em_todos_os_artefatos():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    coolify = (ROOT / "deploy/coolify/docker-compose.yml").read_text()
    assert f"image: cio-portal:{__version__}" in coolify
    assert f"docker build --build-arg CIO_VERSION={__version__} -t cio-portal:{__version__}" in coolify
    assert f"ARG CIO_VERSION={__version__}" in (ROOT / "Dockerfile").read_text()
    assert f"cio-portal:${{CIO_VERSION:-{__version__}}}" in (ROOT / "deploy/docker-compose.yml").read_text()
    guia = (ROOT / "deploy/COOLIFY.md").read_text()
    assert f"cio-portal:{__version__}" in guia and f"cio-portal-v{__version__}.zip" in guia
    for f in ["deploy/COOLIFY.md", "deploy/coolify/docker-compose.yml", "deploy/docker-compose.yml", "README.md",
              "Dockerfile"]:
        texto = (ROOT / f).read_text()
        assert "0.4.0" not in texto and ":latest" not in texto, f


def test_cli_informa_versao(capsys):
    import pytest
    from cio.__main__ import main
    with pytest.raises(SystemExit):
        main(["--version"])
    assert __version__ in capsys.readouterr().out


def test_diagnostico_separa_banco_de_ferramenta_de_backup(settings, monkeypatch, capsys):
    """Sem pg_dump o diagnóstico deve dizer isso, e não acusar erro no banco."""
    import json

    from cio import __main__ as cli
    from cio import backup
    from cio.config import get_settings

    def sem_ferramenta(*_a, **_k):
        raise backup.BackupError("Ferramenta 'pg_dump' não encontrada (instale postgresql-client na imagem).")
    monkeypatch.setattr(backup, "tool_versions", sem_ferramenta)
    get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
    assert cli.cmd_doctor(None) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["banco"] in {"sqlite", "postgres"}
    assert out["ferramentas_backup"]["ok"] is False and "pg_dump" in out["ferramentas_backup"]["erro"]
