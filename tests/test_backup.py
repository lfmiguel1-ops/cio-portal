"""Prioridade 1: backup do banco (SQLite e PostgreSQL) + arquivos originais, restauração de teste e retenção."""
import json
import os
import stat
import tarfile
import time
from pathlib import Path

import pytest

from cio.backup import (BackupError, _pg_env, list_sets, prune, restore_files, run_backup, verify_set,
                        write_protected)
from cio.ingest import run_once
from tests.conftest import PG_URL

PG_BIN = "/usr/lib/postgresql/16/bin"
RESTORE_URL = os.environ.get("CIO_TEST_RESTORE_URL", "")


@pytest.fixture
def bk(settings, conn, fake, tmp_path):
    settings.backup_dir = str(tmp_path / "backups-separado")
    settings.pg_bin_dir = PG_BIN if Path(PG_BIN).exists() else ""
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    fake.add("1", "🔵 Zoho Desk – Resumo diário", "<p>resumo</p>")
    fake.add("2", "🔵 Relatório de reservas — 10/11", "<p>reservas</p>", received_ms=1_794_000_000_100)
    run_once(conn, fake, settings)
    return settings


def test_backup_completo_e_restauracao(bk, conn):
    made = run_backup(conn, bk)
    man = json.loads((bk.backups_dir / made["manifesto"]).read_text())
    assert man["banco"] == conn.dialect and man["contagens"]["reports"] == 2
    assert made["qtd_arquivos"] >= 4  # 2 html + 2 eml
    assert bk.data_dir.resolve() not in bk.backups_dir.resolve().parents  # backup fora de /data
    for name in man["artefatos"]:
        assert stat.S_IMODE((bk.backups_dir / name).stat().st_mode) == 0o600
    res = verify_set(bk, made["conjunto"], reference=conn)
    assert res["ok"], res
    assert res["arquivos_conferidos"] == made["qtd_arquivos"] and res["referencias_sem_arquivo"] == 0
    if conn.is_pg:
        assert res["dump_legivel"] is True
    else:
        assert res["restauracao_completa"] is True and res["contagens_restauradas"]["reports"] == 2
    assert conn.execute("SELECT count(*) FROM audit_log WHERE action IN ('backup','restore_test')").fetchone()[0] == 2


@pytest.mark.skipif(not (PG_URL and RESTORE_URL), reason="restauração completa exige PostgreSQL e banco de teste")
def test_postgres_restauracao_completa_em_banco_de_teste(bk, conn):
    bk.restore_test_database_url = RESTORE_URL
    made = run_backup(conn, bk)
    res = verify_set(bk, made["conjunto"], reference=conn)
    assert res["ok"], res
    assert res["restauracao_completa"] is True and res["contagens_restauradas"]["reports"] >= 2


def test_backup_corrompido_e_detectado(bk, conn):
    made = run_backup(conn, bk)
    db_art = bk.backups_dir / made["banco"]
    os.chmod(db_art, 0o600)
    data = bytearray(db_art.read_bytes())
    data[len(data) // 2] ^= 0xFF
    db_art.write_bytes(bytes(data))
    res = verify_set(bk, made["conjunto"])
    assert not res["ok"] and any("SHA-256" in p for p in res["problemas"])


def test_arquivo_original_adulterado_no_pacote_e_detectado(bk, conn, tmp_path):
    made = run_backup(conn, bk)
    man = json.loads((bk.backups_dir / made["manifesto"]).read_text())
    alvo = next(iter(man["arquivos"]))
    man["arquivos"][alvo]["sha256"] = "0" * 64  # manifesto diz uma coisa, pacote tem outra
    (bk.backups_dir / made["manifesto"]).write_text(json.dumps(man))
    res = verify_set(bk, made["conjunto"])
    assert not res["ok"] and any("arquivo(s) corrompido" in p for p in res["problemas"])


def test_referencia_sem_arquivo_e_apontada(bk, conn):
    conn.execute("UPDATE reports SET original_path='originals/sumiu.html' WHERE zoho_message_id='1'")
    res = verify_set(bk, run_backup(conn, bk)["conjunto"], reference=conn)
    assert res["referencias_sem_arquivo"] == 1 and res["exemplos_sem_arquivo"] == ["originals/sumiu.html"]


def test_retencao_diaria_e_mensal(bk):
    bk.backup_keep_daily, bk.backup_keep_monthly = 3, 12
    for st in ["20260901-020000", "20261001-020000", "20261005-020000"] + \
              [f"202611{d:02d}-020000" for d in range(1, 11)]:
        for ext in ("json", "db.dump", "files.tar.gz"):
            (bk.backups_dir / f"cio-{st}.{ext}").write_text("x")
    (bk.backups_dir / "cio-20261110-030000.files.tmp").write_text("resto de execução interrompida")
    prune(bk)
    assert list_sets(bk) == ["20260901-020000", "20261001-020000", "20261101-020000", "20261108-020000",
                             "20261109-020000", "20261110-020000"]
    assert not list(bk.backups_dir.glob("cio-20261105-*")) and not list(bk.backups_dir.glob("*.tmp"))


def test_restaurar_arquivos_e_bloqueio_de_path_traversal(bk, conn, tmp_path):
    made = run_backup(conn, bk)
    n = restore_files(bk, made["conjunto"], tmp_path / "recuperado")
    assert n == made["qtd_arquivos"]
    # pacote malicioso com '../' não pode escrever fora do destino
    evil = bk.backups_dir / f"cio-{made['conjunto']}.files.tar.gz"
    os.chmod(evil, 0o600)
    payload = tmp_path / "payload.txt"
    payload.write_text("x")
    with tarfile.open(evil, "w:gz") as tar:
        tar.add(payload, arcname="../../fora.txt")
    with pytest.raises(Exception):
        restore_files(bk, made["conjunto"], tmp_path / "rec2")
    assert not (tmp_path / "fora.txt").exists()


def test_originais_ficam_somente_leitura(bk, conn):
    paths = [bk.data_dir / r[0] for r in conn.execute("SELECT original_path FROM reports").fetchall()]
    assert paths and all(stat.S_IMODE(p.stat().st_mode) == 0o440 for p in paths)
    write_protected(paths[0], b"novo")  # regravação controlada (ex.: anexo re-tentado) continua possível
    assert paths[0].read_bytes() == b"novo" and stat.S_IMODE(paths[0].stat().st_mode) == 0o440


def test_conninfo_do_pg_dump_nao_contem_senha():
    conninfo, env = _pg_env("postgresql://cio_app:S3nh4-Secreta@db:5432/cio_portal")
    assert "S3nh4" not in conninfo and env["PGPASSWORD"] == "S3nh4-Secreta"


def test_ferramenta_ausente_gera_erro_claro(bk, conn, monkeypatch):
    if not conn.is_pg:
        pytest.skip("só PostgreSQL usa pg_dump")
    bk.pg_bin_dir = ""
    monkeypatch.setenv("PATH", "/nao-existe")
    with pytest.raises(BackupError, match="postgresql-client"):
        run_backup(conn, bk)


def test_botao_de_backup_do_admin(portal, conn, settings, tmp_path):
    from tests.test_portal_security import csrf_of
    settings.backup_dir = str(tmp_path / "bk-admin")
    settings.pg_bin_dir = PG_BIN if Path(PG_BIN).exists() else ""
    ad = portal["client"]("ad")
    assert ad.post("/admin/backup", data={"csrf_token": csrf_of(ad)}).headers["location"] == "/admin?aviso=backup"
    for _ in range(100):
        if conn.execute("SELECT count(*) FROM audit_log WHERE action='restore_test'").fetchone()[0]:
            break
        time.sleep(0.1)
    assert conn.execute("SELECT count(*) FROM audit_log WHERE action='restore_test'").fetchone()[0] == 1
    assert portal["client"]("ge").post("/admin/backup", data={}).status_code == 404


def test_mesmo_banco_reconhecido_por_host_porta_nome():
    from cio.backup import _same_database
    assert _same_database("postgresql://a:x@db:5432/cio_portal", "postgresql://b:y@DB/cio_portal")
    assert not _same_database("postgresql://a:x@db:5432/cio_portal", "postgresql://a:x@db:5432/cio_restore_test")
    assert _same_database("isto não é uma url", "postgresql://db/cio")  # na dúvida: trata como o mesmo


@pytest.mark.skipif(not PG_URL, reason="só com PostgreSQL")
def test_banco_de_teste_igual_ao_de_producao_nao_e_apagado(bk, conn):
    bk.restore_test_database_url = bk.database_url  # erro de configuração: mesma URL nas duas variáveis
    made = run_backup(conn, bk)
    antes = conn.execute("SELECT count(*) FROM reports").fetchone()[0]
    res = verify_set(bk, made["conjunto"], reference=conn)
    assert not res["ok"] and res["restauracao_completa"] is False
    assert any("banco de produção" in p for p in res["problemas"])
    assert conn.execute("SELECT count(*) FROM reports").fetchone()[0] == antes


def test_verificacao_extrai_no_disco_dos_backups(bk, conn, monkeypatch):
    import tempfile
    usados = []
    original = tempfile.TemporaryDirectory

    def espiao(*a, **k):
        usados.append(k.get("dir"))
        return original(*a, **k)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", espiao)
    made = run_backup(conn, bk)
    assert verify_set(bk, made["conjunto"], reference=conn)["ok"]
    assert usados and all(d == bk.backups_dir for d in usados)
    assert not list(bk.backups_dir.glob(".verifica-*"))  # nada sobra
