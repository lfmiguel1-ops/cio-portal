# Instalação no Coolify (servidor da empresa)

> Guia completo da TI (servidor, rede, DNS, banco, backup, operação e problemas comuns): **`deploy/GUIA_TI.md`**.

Tempo estimado: 30 a 40 minutos, com Luis disponível para os passos 5 e 7.

## 1. Banco PostgreSQL (uma vez)
No PostgreSQL que o Coolify já gerencia, abra um terminal como administrador do banco e rode
`deploy/postgres_setup.sql`. **Antes, troque a senha** na linha `CREATE ROLE`.

```bash
psql -U <admin> -f postgres_setup.sql
```

O script:
- cria o usuário `cio_app`, sem privilégios de administrador;
- cria o banco `cio_portal`, de propriedade desse usuário;
- habilita a extensão padrão `unaccent`, usada pela busca sem acento.

As tabelas são criadas pelo próprio portal na primeira inicialização.

**Backup.** O portal faz sozinho, todo dia às 02:00, um conjunto com **o banco (`pg_dump`) e os arquivos
originais** (e-mails, anexos, PDFs/imagens enviados), com SHA-256 de cada arquivo. Domingo às 03:30 roda um
**teste de restauração** automático. Os conjuntos ficam no volume **`cio-backups`** (separado de `cio-data`),
com retenção de 30 diários + 12 mensais. Resultado em Administração (linha "Backup") e em Auditoria.

A TI precisa:
1. **Copiar o volume `cio-backups` para fora do servidor** (outro disco, storage ou rotina já existente). Um
   backup no mesmo disco não protege contra perda do servidor. Opcional e sem custo: `BACKUP_TO_WORKDRIVE=true`
   envia uma cópia para a pasta privada do WorkDrive.
2. (Recomendado) Criar o banco vazio **`cio_restore_test`** (seção opcional de `postgres_setup.sql`) e definir
   `RESTORE_TEST_DATABASE_URL`: o teste semanal passa a **restaurar de verdade** e comparar as contagens.
3. Manter, se já existir, o backup próprio do PostgreSQL feito pelo Coolify/TI (camada extra).

**Limite de envio:** o portal aceita arquivos de até 25 MB. Se houver limite de tamanho de requisição no
proxy (Traefik/Coolify), deixe-o em 30 MB.

## 2. Imagem do portal (no servidor)
```bash
unzip cio-portal-v0.9.1.zip
```

```bash
cd cio-portal
```

```bash
docker build --build-arg CIO_VERSION=0.9.1 -t cio-portal:0.9.1 .
```

## 3. Recurso no Coolify
- **New Resource › Docker Compose Empty** e cole `deploy/coolify/docker-compose.yml`.
- Ative **Connect to Predefined Network**, para o portal enxergar o PostgreSQL do Coolify.
- **Domains** do serviço `cio-portal`: `https://relatorios.europcar.com.br:8000`. O HTTPS é emitido pelo Coolify.

## 4. Variáveis (Coolify › Environment Variables; marcar as sensíveis como *secret*)

| Variável | Valor |
|---|---|
| `DATABASE_URL` | URL **interna** do PostgreSQL no Coolify, com usuário `cio_app` e banco `cio_portal`. Ex.: `postgresql://cio_app:SENHA@<host-interno>:5432/cio_portal` |
| `CIO_BASE_URL` | `https://relatorios.europcar.com.br` |
| `CIO_SECRET_KEY` | gerar com o comando abaixo |
| `ZOHO_LOGIN_CLIENT_ID` / `ZOHO_LOGIN_CLIENT_SECRET` | do cliente "CIO Portal - Login" (Zoho API Console) |
| `ZOHO_INGEST_CLIENT_ID` / `ZOHO_INGEST_CLIENT_SECRET` | do "Self Client" (Zoho API Console) |
| `ZOHO_INGEST_REFRESH_TOKEN` | gerado no passo 5 |
| `INGEST_ENABLED` | `false` até a aprovação da simulação |

Comando para gerar o `CIO_SECRET_KEY`:

```bash
openssl rand -base64 48
```

Clique em **Deploy** e confira os logs: a linha `Application startup complete` indica que subiu.

## 5. Token de leitura dos e-mails (com Luis)
1. Luis entra no Zoho API Console › Self Client › **Generate Code** e informa os escopos:

   ```
   ZohoMail.accounts.READ,ZohoMail.folders.READ,ZohoMail.messages.READ,WorkDrive.files.READ,WorkDrive.files.CREATE,WorkDrive.team.READ,WorkDrive.teamfolders.READ
   ```

   Duração: 10 minutos.
2. Logo em seguida, no terminal do container (Coolify › Terminal):

   ```bash
   python -m cio zoho-token --code <código> --mostrar
   ```

3. Copie o token exibido para `ZOHO_INGEST_REFRESH_TOKEN` (secret) e faça **Redeploy**. Não envie o token por e-mail ou chat.

## 6. Primeiro administrador
No Coolify › Terminal do container:

```bash
python -m cio add-user luis.miguel@europcar.com.br admin --group diretoria
```

## 6b. Diagnóstico
No Coolify › Terminal do container:

```bash
python -m cio diagnostico
```

Confere banco, versão do `pg_dump` em relação ao servidor, pastas graváveis, backup separado dos dados e
credenciais configuradas (sem exibir segredos). Tudo deve vir `true`/`ok`.

## 7. Simulação (nada é gravado)
No Coolify › Terminal do container:

```bash
python -m cio ingest --dry-run --since 2026-10-01
```

Luis revisa o resultado. Com a aprovação, rode:

```bash
python -m cio ingest --since 2026-10-01 --batch 50
```

Depois disso, `INGEST_ENABLED=true` e **Redeploy**.

## Antes de liberar para a equipe (checklist)
- [ ] Domínio com **DMARC `p=reject`** (ou `quarantine`) para europcar.com.br — confirmar com quem administra o DNS.
- [ ] `python -m cio backup` executado com `"ok": true`; volume `cio-backups` copiado para fora do servidor.
- [ ] `RESTORE_TEST_DATABASE_URL` configurada e teste com `"restauracao_completa": true`.
- [ ] Primeiro login real pelo "Entrar com Zoho" funcionando.
- [ ] Simulação (`--dry-run`) revisada: grupos sugeridos e destinatários não cadastrados conferidos.
- [ ] Um relatório real aberto no navegador: tabelas, imagens e links conferidos.
- [ ] Pessoas que recebem relatórios cadastradas com o grupo certo (Usuários).

## Recuperação (restaurar um backup)
Com o portal **parado** (Coolify › Stop):

1. Escolha o conjunto: `ls /backups` no container (ou no volume `cio-backups`) — ex.: `cio-20261120-020000`.
2. **Banco** — no container do portal (Coolify › Terminal), com o banco de destino vazio ou para substituir:

   ```bash
   pg_restore --clean --if-exists --no-owner --no-privileges --dbname="$DATABASE_URL" /backups/cio-20261120-020000.db.dump
   ```

3. **Arquivos** — extrai conferindo o SHA-256 de cada um:

   ```bash
   python -m cio restaurar-arquivos --conjunto 20261120-020000 --destino /tmp/restauro
   ```

   Depois copie o conteúdo de `/tmp/restauro/originals` para `/data/originals`.
4. Inicie o portal e rode `python -m cio diagnostico` e `python -m cio verificar-backup`.

## Rollback (voltar de versão)
- **Parar a captura** sem parar o portal: `INGEST_ENABLED=false` e Redeploy.
- **Voltar de versão:** cada versão é construída com a sua tag (`cio-portal:0.9.1`, a próxima `cio-portal:0.9.2`…).
  Antes de atualizar, rode **Backup + teste de restauração** no Admin. Para voltar, troque a tag no compose do
  Coolify pela versão anterior (a imagem continua no servidor; confira com `docker images cio-portal`) e Redeploy.
- **Migrações de banco:** as migrações só acrescentam tabelas e colunas com valor padrão, então a versão anterior
  dentro da mesma linha 0.9.x continua funcionando sobre o banco migrado. Para voltar a uma versão **anterior à
  introdução dos grupos de acesso (0.7)**, restaure também o backup do banco feito antes da atualização.
- **Remover:** apague o recurso no Coolify, o banco `cio_portal` e o usuário `cio_app`, e revogue os clientes no
  Zoho API Console.

O portal **nunca altera, move ou apaga e-mails**.
