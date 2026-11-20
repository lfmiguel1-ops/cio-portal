"""Backups: banco (PostgreSQL via pg_dump ou SQLite) + arquivos originais, retenção e teste de restauração.

Cada execução gera um CONJUNTO com o mesmo carimbo de data/hora em `BACKUP_DIR` (volume separado de /data):
  cio-AAAAMMDD-HHMMSS.db.dump      PostgreSQL (pg_dump --format=custom)   | .db.sqlite.gz (SQLite)
  cio-AAAAMMDD-HHMMSS.files.tar.gz arquivos originais (e-mails, anexos, envios manuais)
  cio-AAAAMMDD-HHMMSS.json         manifesto: versão, SHA-256 dos artefatos e de cada arquivo, contagens

O teste de restauração confere o SHA-256 de tudo, lê o dump (pg_restore --list), extrai os arquivos com
proteção contra path traversal e, se RESTORE_TEST_DATABASE_URL estiver configurada (banco vazio só para
teste), restaura o dump de verdade e compara as contagens. Também aponta arquivos referenciados no banco
que não estão no backup. Nenhum segredo é gravado em log, manifesto ou linha de comando.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import Settings
from .db import Database, audit, connect

log = logging.getLogger("cio.backup")
COUNT_TABLES = ("reports", "users", "groups", "report_groups", "classification_rules", "mail_sources", "audit_log")


class BackupError(RuntimeError):
    pass


# ---------------------------------------------------------------- utilitários
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def protect_file(path: Path) -> None:
    """Originais ficam somente-leitura (o app não os sobrescreve por engano)."""
    try:
        os.chmod(path, 0o440)
    except OSError:
        pass


def write_protected(path: Path, data: bytes) -> None:
    """Grava um original e o deixa somente-leitura (remove antes um arquivo anterior incompleto)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    path.write_bytes(data)
    protect_file(path)


def _pg_env(url: str) -> tuple[str, dict]:
    """Converte a URL em conninfo SEM senha; a senha vai só na variável de ambiente PGPASSWORD."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    params = conninfo_to_dict(url)
    env = dict(os.environ)
    password = params.pop("password", None)
    if password:
        env["PGPASSWORD"] = str(password)
    env.setdefault("PGCONNECT_TIMEOUT", "15")
    return make_conninfo(**params), env


def _same_search_path(target_url: str, source_url: str) -> str:
    """Conecta ao banco restaurado com o mesmo search_path (schema) do banco de origem."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    src = conninfo_to_dict(source_url or "")
    if not src.get("options"):
        return target_url
    dst = conninfo_to_dict(target_url)
    dst["options"] = src["options"]
    return make_conninfo(**dst)


def _pg_tool(s: Settings, name: str) -> str:
    candidate = Path(s.pg_bin_dir) / name if s.pg_bin_dir else None
    if candidate and candidate.exists():
        return str(candidate)
    found = shutil.which(name)
    if not found:
        raise BackupError(f"Ferramenta '{name}' não encontrada (instale postgresql-client na imagem).")
    return found


def _run(cmd: list[str], env: dict, timeout: int = 3600) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        # stderr do pg_dump não contém a senha (ela não está na linha de comando nem na conninfo)
        raise BackupError(f"{Path(cmd[0]).name} falhou: {proc.stderr.strip()[:500]}")
    return proc


def counts(db: Database) -> dict:
    return {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in COUNT_TABLES}


def tool_versions(s: Settings, db: Database) -> dict:
    """pg_dump precisa ser da mesma versão principal do servidor ou mais nova."""
    if not db.is_pg:
        return {"banco": "sqlite"}
    server = int(db.execute("SHOW server_version_num").fetchone()[0]) // 10000
    out = subprocess.run([_pg_tool(s, "pg_dump"), "--version"], capture_output=True, text=True).stdout
    client = int("".join(ch for ch in out.split()[-1].split(".")[0] if ch.isdigit()) or 0)
    return {"servidor": server, "pg_dump": client, "ok": client >= server}


# ---------------------------------------------------------------- backup
def _backup_database(db: Database, s: Settings, stamp: str) -> Path:
    if db.is_pg:
        dest = s.backups_dir / f"cio-{stamp}.db.dump"
        tmp = dest.with_suffix(".tmp")
        conninfo, env = _pg_env(s.database_url)
        _run([_pg_tool(s, "pg_dump"), "--format=custom", "--no-owner", "--no-privileges", "--compress=6",
              f"--file={tmp}", f"--dbname={conninfo}"], env)
        tmp.replace(dest)
    else:
        dest = s.backups_dir / f"cio-{stamp}.db.sqlite.gz"
        raw = s.backups_dir / f"cio-{stamp}.db.tmp"
        target = sqlite3.connect(str(raw))
        try:
            db.raw.backup(target)
        finally:
            target.close()
        with open(raw, "rb") as src, gzip.open(dest, "wb", compresslevel=6) as out:
            shutil.copyfileobj(src, out)
        raw.unlink()
    os.chmod(dest, 0o600)
    return dest


def _backup_files(s: Settings, stamp: str) -> tuple[Path, dict]:
    dest = s.backups_dir / f"cio-{stamp}.files.tar.gz"
    tmp = dest.with_suffix(".tmp")
    manifest: dict[str, dict] = {}
    base = s.originals_dir
    with tarfile.open(tmp, "w:gz", compresslevel=6) as tar:
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            rel = str(path.relative_to(s.data_dir))
            manifest[rel] = {"sha256": _sha256(path), "bytes": path.stat().st_size}
            tar.add(path, arcname=rel, recursive=False)
    tmp.replace(dest)
    os.chmod(dest, 0o600)
    return dest, manifest


def run_backup(db: Database, s: Settings, uploader=None) -> dict:
    """Gera um conjunto completo (banco + arquivos + manifesto) e aplica a retenção."""
    s.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    before = counts(db)
    db_file = _backup_database(db, s, stamp)
    files_file, files_manifest = _backup_files(s, stamp)
    manifest = {
        "versao_portal": __version__, "criado_em": stamp, "banco": db.dialect,
        "artefatos": {p.name: {"sha256": _sha256(p), "bytes": p.stat().st_size} for p in (db_file, files_file)},
        "contagens": before, "arquivos": files_manifest,
    }
    man_path = s.backups_dir / f"cio-{stamp}.json"
    man_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(man_path, 0o600)
    if uploader:
        for p in (db_file, files_file, man_path):
            try:
                uploader(p)
            except Exception as e:  # cópia externa é complementar
                log.warning("Cópia externa do backup falhou para %s: %s", p.name, type(e).__name__)
    removed = prune(s)
    size = sum(v["bytes"] for v in manifest["artefatos"].values())
    audit(db, "backup", target=stamp, details=f"{db.dialect}; {len(files_manifest)} arquivos; {size} bytes; "
                                              f"removidos {removed}")
    return {"conjunto": stamp, "banco": db_file.name, "arquivos": files_file.name, "manifesto": man_path.name,
            "qtd_arquivos": len(files_manifest), "bytes": size}


# ---------------------------------------------------------------- retenção
def list_sets(s: Settings) -> list[str]:
    return sorted(p.name[4:19] for p in s.backups_dir.glob("cio-*.json"))


def prune(s: Settings) -> int:
    """Mantém os últimos BACKUP_KEEP_DAILY conjuntos + o primeiro de cada mês (BACKUP_KEEP_MONTHLY meses)."""
    sets = list_sets(s)
    keep = set(sets[-s.backup_keep_daily:]) if s.backup_keep_daily > 0 else set()
    monthly: dict[str, str] = {}
    for st in sets:
        monthly.setdefault(st[:6], st)
    keep |= set(sorted(monthly.values())[-s.backup_keep_monthly:]) if s.backup_keep_monthly > 0 else set()
    removed = 0
    for st in sets:
        if st not in keep:
            for p in s.backups_dir.glob(f"cio-{st}.*"):
                p.unlink()
            removed += 1
    # artefatos órfãos de execuções interrompidas
    for p in s.backups_dir.glob("cio-*.tmp"):
        p.unlink()
    return removed


# ---------------------------------------------------------------- restauração de teste
def _referenced_files(db: Database) -> set[str]:
    refs = set()
    for sql in ("SELECT original_path FROM reports WHERE original_path<>''",
                "SELECT file_path FROM reports WHERE file_path<>''",
                "SELECT local_path FROM attachments WHERE local_path<>''"):
        refs |= {r[0] for r in db.execute(sql).fetchall()}
    return refs


def _same_database(a: str, b: str) -> bool:
    """True se as duas URLs apontam para o mesmo banco (host, porta e nome), ignorando usuário e senha."""
    from psycopg.conninfo import conninfo_to_dict

    def key(url: str):
        d = conninfo_to_dict(url)
        host = (d.get("host") or d.get("hostaddr") or "localhost").lower()
        return host, str(d.get("port") or "5432"), d.get("dbname") or d.get("user") or ""
    try:
        return key(a) == key(b)
    except Exception:
        return True  # na dúvida, trata como o mesmo banco e não apaga nada


def _empty_restore_target(s, listing: str) -> None:
    """Esvazia, no banco de TESTE de restauração, os esquemas presentes no dump.

    Uma cópia anterior com estrutura diferente (ex.: chaves estrangeiras de uma versão mais nova) faria o
    pg_restore --clean falhar ao derrubar restrições. Remove tabelas (com sequências e gatilhos), funções e
    configurações de busca criadas pelo portal; objetos de extensões (unaccent) ficam. Nunca roda se o banco
    de teste for o mesmo de DATABASE_URL."""
    if _same_database(s.restore_test_database_url, s.database_url):
        raise BackupError("RESTORE_TEST_DATABASE_URL aponta para o banco de produção: teste real não executado.")
    import re as _re
    schemas = sorted({m.group(1) for m in _re.finditer(r"TABLE DATA (\S+) ", listing)})
    if not schemas:
        return
    q = lambda name: '"' + name.replace('"', '""') + '"'  # noqa: E731
    not_ext = "NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid={oid} AND d.deptype='e')"
    target = connect(_same_search_path(s.restore_test_database_url, s.database_url))
    try:
        for schema, table in target.execute(
                "SELECT schemaname, tablename FROM pg_tables WHERE schemaname = ANY(?)", (schemas,)).fetchall():
            target.execute(f"DROP TABLE IF EXISTS {q(schema)}.{q(table)} CASCADE")
        for schema, ident in target.execute(
                "SELECT n.nspname, p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname = ANY(?) AND " + not_ext.format(oid="p.oid"), (schemas,)).fetchall():
            target.execute(f"DROP FUNCTION IF EXISTS {ident} CASCADE")
        for schema, name in target.execute(
                "SELECT n.nspname, c.cfgname FROM pg_ts_config c JOIN pg_namespace n ON n.oid=c.cfgnamespace "
                "WHERE n.nspname = ANY(?) AND " + not_ext.format(oid="c.oid"), (schemas,)).fetchall():
            target.execute(f"DROP TEXT SEARCH CONFIGURATION IF EXISTS {q(schema)}.{q(name)} CASCADE")
    finally:
        target.close()


def verify_set(s: Settings, stamp: str | None = None, reference: Database | None = None) -> dict:
    sets = list_sets(s)
    if not sets:
        raise BackupError("Nenhum backup encontrado.")
    stamp = stamp or sets[-1]
    man_path = s.backups_dir / f"cio-{stamp}.json"
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    result: dict = {"conjunto": stamp, "banco": manifest["banco"], "versao_portal": manifest.get("versao_portal")}

    # 1) integridade dos artefatos
    for name, meta in manifest["artefatos"].items():
        p = s.backups_dir / name
        if not p.exists():
            problems.append(f"artefato ausente: {name}")
        elif _sha256(p) != meta["sha256"]:
            problems.append(f"SHA-256 diferente: {name}")
    if problems:
        return {**result, "ok": False, "problemas": problems}

    # 2) banco
    db_name = next(n for n in manifest["artefatos"] if ".db." in n)
    db_path = s.backups_dir / db_name
    if manifest["banco"] == "postgres":
        conninfo, env = _pg_env(s.database_url) if s.database_url else ("", dict(os.environ))
        import re as _re
        listing = _run([_pg_tool(s, "pg_restore"), "--list", str(db_path)], env).stdout
        missing = [t for t in COUNT_TABLES if not _re.search(rf"TABLE DATA \S+ {t} ", listing)]
        if missing:
            problems.append(f"tabelas ausentes no dump: {', '.join(missing)}")
        result["dump_legivel"] = not missing
        if s.restore_test_database_url and _same_database(s.restore_test_database_url, s.database_url):
            problems.append("RESTORE_TEST_DATABASE_URL aponta para o banco de produção: teste real não executado")
            result["restauracao_completa"] = False
        elif s.restore_test_database_url:
            tconninfo, tenv = _pg_env(s.restore_test_database_url)
            # Extensões (unaccent) já existem no banco de teste e só um superusuário as recria: ficam de fora.
            with tempfile.NamedTemporaryFile("w", suffix=".list", delete=False) as lf:
                lf.write("\n".join(l for l in listing.splitlines() if " EXTENSION " not in l))
                list_file = lf.name
            try:
                try:
                    _empty_restore_target(s, listing)
                except BackupError:
                    raise
                except Exception as e:  # conexão/permissão: falha clara, e o .list é apagado no finally
                    raise BackupError(f"não foi possível preparar o banco de teste: {type(e).__name__}") from e
                _run([_pg_tool(s, "pg_restore"), "--clean", "--if-exists", "--no-owner", "--no-privileges",
                      "--exit-on-error", f"--use-list={list_file}", f"--dbname={tconninfo}", str(db_path)], tenv)
            finally:
                os.unlink(list_file)
            restored = connect(_same_search_path(s.restore_test_database_url, s.database_url))
            try:
                got = counts(restored)
            finally:
                restored.close()
            result["contagens_restauradas"] = got
            short = [t for t, n in manifest["contagens"].items() if got.get(t, 0) < n]
            if short:
                problems.append(f"restauração com menos registros em: {', '.join(short)}")
            result["restauracao_completa"] = not short
        else:
            result["restauracao_completa"] = None  # sem banco de teste configurado: só leitura do dump
    else:
        with tempfile.TemporaryDirectory(dir=s.backups_dir, prefix=".verifica-") as d:
            target = Path(d) / "restore.db"
            with gzip.open(db_path, "rb") as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            r = connect(target)
            try:
                if r.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    problems.append("integrity_check do SQLite falhou")
                got = counts(r)
                fts = r.execute("SELECT count(*) FROM reports_fts").fetchone()[0]
            finally:
                r.close()
        result["contagens_restauradas"] = got
        if fts != got["reports"]:
            problems.append("índice de busca inconsistente")
        if any(got.get(t, 0) < n for t, n in manifest["contagens"].items()):
            problems.append("restauração com menos registros que o backup registrou")
        result["restauracao_completa"] = not problems

    # 3) arquivos: extração segura + SHA-256 de cada um
    files_name = next(n for n in manifest["artefatos"] if ".files." in n)
    # Extrai no disco dos backups, não no /tmp (memória do container, limitado no compose).
    with tempfile.TemporaryDirectory(dir=s.backups_dir, prefix=".verifica-") as d, \
            tarfile.open(s.backups_dir / files_name, "r:gz") as tar:
        tar.extractall(d, filter="data")  # bloqueia caminhos absolutos, '..' e links perigosos
        bad = [rel for rel, meta in manifest["arquivos"].items()
               if not (Path(d) / rel).is_file() or _sha256(Path(d) / rel) != meta["sha256"]]
    if bad:
        problems.append(f"{len(bad)} arquivo(s) corrompido(s) ou ausente(s) no pacote")
    result["arquivos_conferidos"] = len(manifest["arquivos"]) - len(bad)

    # 4) consistência com o banco atual: arquivos referenciados que não entraram no backup
    if reference is not None:
        missing_refs = sorted(_referenced_files(reference) - set(manifest["arquivos"]))
        result["referencias_sem_arquivo"] = len(missing_refs)
        if missing_refs:
            result["exemplos_sem_arquivo"] = missing_refs[:5]
        audit(reference, "restore_test", target=stamp,
              details=f"ok={not problems} problemas={len(problems)} sem_arquivo={len(missing_refs)}")
    return {**result, "ok": not problems, "problemas": problems}


def restore_files(s: Settings, stamp: str, target: Path) -> int:
    """Extrai os arquivos de um conjunto em `target` (para recuperação), conferindo o SHA-256."""
    manifest = json.loads((s.backups_dir / f"cio-{stamp}.json").read_text(encoding="utf-8"))
    files_name = next(n for n in manifest["artefatos"] if ".files." in n)
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(s.backups_dir / files_name, "r:gz") as tar:
        tar.extractall(target, filter="data")
    bad = [rel for rel, meta in manifest["arquivos"].items() if _sha256(target / rel) != meta["sha256"]]
    if bad:
        raise BackupError(f"{len(bad)} arquivo(s) com SHA-256 diferente após extração.")
    return len(manifest["arquivos"])


def last_backup_info(s: Settings) -> dict | None:
    sets = list_sets(s)
    if not sets:
        return None
    man = json.loads((s.backups_dir / f"cio-{sets[-1]}.json").read_text(encoding="utf-8"))
    return {"conjunto": sets[-1], "arquivos": len(man["arquivos"]), "total": len(sets),
            "bytes": sum(v["bytes"] for v in man["artefatos"].values())}
