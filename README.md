# Central de Inteligência Operacional

O portal interno lê os relatórios diários enviados ao Zoho Mail e os cataloga e indexa. Depois os disponibiliza para pesquisa e leitura por funcionários autorizados. Funciona **sem Claude e sem licença WorkDrive** para quem consulta.

## Arquitetura
- **Python 3.12 + FastAPI**, páginas renderizadas no servidor (Jinja2), sem JavaScript de terceiros.
- **PostgreSQL** em produção: busca em português que ignora acentos e entende plural, com `unaccent` + dicionário `portuguese`. O backup segue a rotina da TI.
- **SQLite (FTS5)** apenas para desenvolvimento e testes rápidos, quando `DATABASE_URL` está vazio.
- **Ingestão**: o próprio processo consulta o Zoho Mail (somente leitura) a cada 10 minutos. Cada mensagem é registrada uma única vez, por `messageId` e SHA-256. O checkpoint é persistente e as falhas são retentadas.
- **Originais**: uma cópia local fica em `/data/originals` e outra vai para a pasta privada do WorkDrive (`<área>/<ano>/<mês>`).
- **Login**: Zoho OIDC (PKCE, state e nonce). Só entram e-mails cadastrados por um administrador.
- **Autorização**: perfil (operacional, gestor ou admin), área e confidencialidade, aplicados no SQL de toda listagem, busca, detalhe e download.
- **Backup** diário às 02:00 (banco via `pg_dump` ou SQLite + arquivos originais, com SHA-256) em volume separado, retenção 30 diários + 12 mensais, e teste de restauração semanal.

## Estrutura
```
cio/config.py     configuração via variáveis de ambiente
cio/schema.sql    esquema, áreas e regras iniciais
cio/zoho.py       cliente Zoho Mail/WorkDrive (token, retentativas)
cio/ingest.py     ingestão idempotente, versões, erros, simulação
cio/sanitize.py   sanitização de HTML não confiável e extração de texto
cio/classify.py   regras determinísticas (sem IA)
cio/access.py     RBAC e busca FTS5
cio/auth.py       sessões, CSRF, limite de taxa, OIDC
cio/backup.py     backup, retenção e teste de restauração
cio/app.py        rotas web
tests/            testes automatizados (dados sintéticos)
deploy/           docker-compose + Caddy (HTTPS)
```

## Desenvolvimento
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
```

Para rodar os mesmos testes contra o PostgreSQL:

```bash
CIO_TEST_DATABASE_URL='postgresql://cio_app@/cio_portal?host=/tmp&port=5432' python -m pytest -q
```

```bash
CIO_ENV=dev CIO_DATA_DIR=./data python -m cio add-user voce@europcar.com.br admin
```

```bash
CIO_ENV=dev CIO_DATA_DIR=./data python -m cio serve
```

Em `CIO_ENV=dev`, a tela de login oferece um **login simulado**. Essa rota não existe em produção.

## Comandos
```bash
python -m cio ingest --dry-run --since 2026-10-01 --batch 50   # simulação: lista o que seria importado, sem gravar
python -m cio ingest --since 2026-10-01 --batch 50             # importação histórica em lotes
python -m cio backup                                            # backup (banco + arquivos) + teste de restauração
python -m cio verificar-backup [--conjunto AAAAMMDD-HHMMSS]       # teste de restauração
python -m cio restaurar-arquivos --conjunto ... --destino ...    # recuperação dos arquivos
python -m cio diagnostico                                       # checagens de homologação
python -m cio --version
python -m cio add-user email@europcar.com.br gestores
```

## Implantação no Coolify
Ver **`deploy/COOLIFY.md`** (procedimento oficial) e `deploy/postgres_setup.sql`.

## Implantação alternativa (Docker Compose + Caddy, sem Coolify)
1. Crie o registro de DNS do subdomínio apontando para o servidor e libere as portas 80 e 443.
2. Gere a configuração:

   ```bash
   cd deploy && cp ../.env.example .env && chmod 600 .env
   ```

   Depois preencha os valores no `.env`.
3. Suba os containers:

   ```bash
   docker compose up -d --build
   ```

4. Gere o refresh token de ingestão. No Zoho API Console › Self Client › Generate Code, informe os escopos do `.env.example` e duração de 10 min. Depois, no servidor:

   ```bash
   docker compose run --rm -v "$PWD/.env:/run/cio/.env" app python -m cio zoho-token --code <código>
   ```

   O token vai direto para o `.env` e não aparece na tela.
5. Cadastre o primeiro administrador:

   ```bash
   docker compose exec app python -m cio add-user admin@europcar.com.br admin
   ```

6. Rode a simulação da importação histórica:

   ```bash
   docker compose exec app python -m cio ingest --dry-run --since 2026-10-01
   ```

   Revise o resultado, depois importe e ative `INGEST_ENABLED=true`.

### Rollback
- Versão atual: **0.9.2** (`cio/__init__.py`). Construa sempre com a tag da versão. Para voltar à versão anterior:

  ```bash
  CIO_VERSION=<tag-anterior> docker compose up -d
  ```

  Se houve migração de banco, restaure também o backup do dia.
- Para parar a ingestão sem parar o portal, defina `INGEST_ENABLED=false` e reinicie.
- O portal **nunca altera, move ou apaga e-mails**. Desligar o portal não afeta o Zoho Mail.

## Segurança
- O HTML de e-mail é tratado como **não confiável**. Ele passa por uma allowlist de tags e atributos e é exibido num iframe `sandbox` com CSP `default-src 'none'`. Imagens remotas ficam bloqueadas.
- Downloads saem sempre como anexo `application/octet-stream`, com verificação contra path traversal.
- O cookie `__Host-sid` é Secure, HttpOnly e SameSite=Lax, e todo POST exige CSRF. A sessão expira após 8 h sem uso e dura no máximo 7 dias.
- Login, busca e download têm limite de taxa.
- Tokens e segredos ficam só em variáveis de ambiente. Os logs não guardam conteúdo dos relatórios e guardam IPs como hash.
- Um ID inexistente e um ID sem permissão recebem a mesma resposta 404.
