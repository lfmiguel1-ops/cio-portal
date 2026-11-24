# Guia da TI: instalação e operação do portal de relatórios

- **Sistema:** Central de Inteligência Operacional (CIO Portal), versão 0.9.2
- **Endereço:** `https://relatorios.europcar.com.br`
- **Responsável pelo negócio:** Luis Fernando Ambrosio Miguel (administrador do portal e do Zoho)

Este guia reúne tudo o que a TI precisa para preparar o servidor e o banco, instalar, operar e recuperar
o portal. Os comandos estão prontos para copiar. **Nenhuma senha ou token deve ser enviado por e-mail ou chat**:
eles são digitados direto no Coolify, como variável secreta.

---

## 1. Visão geral

| Item | Como é |
|---|---|
| O que é | Portal web interno para consultar os relatórios diários que chegam por e-mail (Zoho Mail) e PDFs/imagens enviados manualmente. |
| Quem usa | Funcionários e franqueados com e-mail `@europcar.com.br`, no computador e no celular. Login pela conta Zoho da empresa. |
| Onde roda | Servidor da empresa, no **Coolify**, em **um container Docker** (`cio-portal:0.9.2`). |
| Banco | O **PostgreSQL já existente** (versão 13 ou superior), com um banco e um usuário exclusivos. |
| Arquivos | Dois volumes Docker: `cio-data` (originais) e `cio-backups` (backups). |
| Custo | Nenhum. Sem licenças, sem serviços pagos, sem CDN. Certificado HTTPS gratuito (Let's Encrypt, via Coolify). |
| Acesso ao Zoho | Só **leitura** dos e-mails. O portal **nunca altera, move ou apaga e-mails**. Grava apenas na pasta privada do WorkDrive "CIO - Relatorios". |

```
Internet (usuários)
   │ HTTPS 443
   ▼
Coolify (proxy + certificado)
   │ porta interna 8000
   ▼
container cio-portal ──► PostgreSQL existente (banco cio_portal)
   │                 ──► volume cio-data     (/data, originais)
   │                 ──► volume cio-backups  (/backups) ──► cópia fora do servidor (TI)
   ▼ HTTPS 443
Zoho (login, Mail, WorkDrive)
```

---

## 2. Quem faz o quê

| Tarefa | TI | Luis |
|---|:-:|:-:|
| Servidor, rede e portas (seção 3) | ✔ | |
| Pedido de DNS ao provedor externo (seção 4) | ✔ (envia) | ✔ (aprova) |
| Banco e usuário no PostgreSQL (seção 5) | ✔ | |
| Construir a imagem e criar o recurso no Coolify (seções 6–7) | ✔ | |
| Preencher as variáveis secretas do Zoho (seção 8) | | ✔ |
| Gerar o token de leitura dos e-mails (seção 9) | apoio no terminal | ✔ |
| Primeiro administrador, diagnóstico, simulação (seções 10–11) | ✔ | ✔ (revisa) |
| Cópia dos backups para fora do servidor (seção 12) | ✔ | |
| Monitoramento, atualizações e recuperação (seções 13–15) | ✔ | avisado |
| Cadastro de usuários, grupos e regras | | ✔ (pelo próprio portal) |

---

## 3. Servidor e rede

### 3.1 Recursos
O portal é leve. Num servidor com Coolify já em uso, reserve para ele:

- **CPU/RAM:** 1 vCPU e 1 GB de RAM livres (uso típico: abaixo de 300 MB).
- **Disco:** 20 GB livres para começar. Estimativa: cerca de 1 a 3 GB por ano de relatórios, mais os backups
  (30 diários + 12 mensais). Acompanhe o uso do volume `cio-backups` nos primeiros meses.
- **Relógio sincronizado (NTP).** O login e os horários dos relatórios dependem disso.

### 3.2 Portas de entrada (internet → servidor)
- **80/TCP e 443/TCP** até o proxy do Coolify. A porta 80 serve só para emitir e renovar o certificado.
- A porta interna **8000** do container **não** deve ser publicada; o Coolify faz o encaminhamento.
- O PostgreSQL **não** deve ficar exposto à internet.

### 3.3 Saídas HTTPS (servidor → internet, porta 443)

| Destino | Para quê | Quando |
|---|---|---|
| `accounts.zoho.com` | login e renovação do token | sempre |
| `mail.zoho.com` | leitura dos e-mails | sempre |
| `www.zohoapis.com` | API do WorkDrive (cópia na pasta privada) | sempre |
| `acme-v02.api.letsencrypt.org` | certificado HTTPS (Coolify) | sempre |
| `deb.debian.org`, `apt.postgresql.org`, `www.postgresql.org` | pacotes do sistema e cliente PostgreSQL 17 | só na construção da imagem |
| `pypi.org`, `files.pythonhosted.org` | bibliotecas Python | só na construção da imagem |
| `registry-1.docker.io`, `production.cloudflare.docker.com` | imagem base `python:3.12-slim-bookworm` | só na construção da imagem |

### 3.4 IP público
Precisamos do **IP público fixo** do servidor para o pedido de DNS.

---

## 4. DNS (provedor externo)

Enviar ao provedor de DNS de `europcar.com.br` (o texto pronto em inglês está em `deploy/DNS_REQUEST_EN.md`):

| Tipo | Nome | Valor | TTL |
|---|---|---|---|
| A | `relatorios.europcar.com.br` | IP público do servidor | 3600 |

- Se o domínio tiver registro **CAA**, ele precisa permitir `letsencrypt.org`.
- **Não alterar** MX, SPF, DKIM e DMARC.

Para conferir depois que o registro estiver ativo:

```bash
nslookup relatorios.europcar.com.br
```

**Recomendação de segurança (sem custo):** confirmar com quem administra o DNS que o DMARC de
`europcar.com.br` está em `p=reject` ou `p=quarantine`. O portal só aceita relatórios com DMARC aprovado,
e essa política dificulta e-mails falsos em nome da empresa.

---

## 5. Banco de dados (PostgreSQL existente)

### 5.1 Criar banco e usuário (uma vez)
Arquivo: `deploy/postgres_setup.sql`. **Antes de rodar, troque `TROQUE-ESTA-SENHA`** por uma senha forte.
Para gerar uma:

```bash
openssl rand -base64 32 | tr -d '/+=' | cut -c1-32
```

Rode com um usuário administrador do PostgreSQL (por exemplo, o do Coolify):

```bash
psql -U <usuario-admin> -h <host> -f postgres_setup.sql
```

O script cria:
- o usuário **`cio_app`**, sem privilégios de administrador (não cria bancos nem usuários);
- o banco **`cio_portal`**, de propriedade de `cio_app`, sem acesso público;
- a extensão padrão **`unaccent`** (busca sem acento: "caucao" encontra "caução");
- o banco **`cio_restore_test`** (opcional, recomendado), vazio, usado só pelo teste semanal de restauração.
  O portal **apaga e recria o conteúdo desse banco** a cada teste; não use para mais nada. Por segurança, ele
  se recusa a fazer isso se o endereço apontar para o mesmo banco `cio_portal`.

As tabelas são criadas pelo próprio portal na primeira inicialização.

### 5.2 Conferir
```bash
psql -U cio_app -h <host> -d cio_portal -c "select current_user, version();"
```

### 5.3 Endereço de conexão
O portal usa o endereço **interno** da rede do Coolify:

```
postgresql://cio_app:<SENHA>@<host-interno>:5432/cio_portal
```

Se a senha tiver caracteres especiais (`@ : / ? # %`), use só letras e números ou codifique-os na URL.

---

## 6. Construir a imagem (no servidor)

Copie `cio-portal-v0.9.2.zip` para o servidor e rode cada comando:

```bash
unzip cio-portal-v0.9.2.zip
```

```bash
cd cio-portal
```

```bash
docker build --build-arg CIO_VERSION=0.9.2 -t cio-portal:0.9.2 .
```

Conferir:

```bash
docker run --rm cio-portal:0.9.2 python -m cio --version
```

Deve responder `cio-portal 0.9.2`. A imagem roda como usuário sem privilégios (uid 10001) e inclui o
`pg_dump`/`pg_restore` 17, compatível com servidores PostgreSQL 13 a 17.

---

## 7. Criar o recurso no Coolify

1. **New Resource › Docker Compose Empty** e cole o conteúdo de `deploy/coolify/docker-compose.yml`.
2. Ative **Connect to Predefined Network**, para o portal alcançar o PostgreSQL.
3. Em **Domains** do serviço `cio-portal`: `https://relatorios.europcar.com.br:8000`
   (o `:8000` só indica a porta interna; o acesso público continua em `https://` na 443).
4. Se o proxy (Traefik) tiver limite de tamanho de requisição, deixe em **30 MB** (o portal aceita envios de até 25 MB).
5. **Não clique em Deploy ainda.** Preencha antes as variáveis da seção 8.

O compose já vem endurecido: sistema de arquivos somente leitura, sem capacidades extras do Linux,
`no-new-privileges`, verificação de saúde em `/saude` e reinício automático.

---

## 8. Variáveis de ambiente (Coolify › Environment Variables)

Marque como **secret** todas as linhas com 🔒.

| Variável | Quem preenche | Valor |
|---|---|---|
| `DATABASE_URL` 🔒 | TI | endereço da seção 5.3 |
| `CIO_BASE_URL` | TI | `https://relatorios.europcar.com.br` |
| `CIO_SECRET_KEY` 🔒 | TI | gerar com o comando abaixo (mínimo 32 caracteres) |
| `RESTORE_TEST_DATABASE_URL` 🔒 | TI | mesmo endereço, terminando em `/cio_restore_test` (recomendado) |
| `BACKUP_TO_WORKDRIVE` | TI + Luis | `false` (padrão) ou `true` para copiar os backups na pasta privada do WorkDrive |
| `ZOHO_LOGIN_CLIENT_ID` | Luis | cliente "CIO Portal - Login" (Zoho API Console) |
| `ZOHO_LOGIN_CLIENT_SECRET` 🔒 | Luis | idem |
| `ZOHO_INGEST_CLIENT_ID` | Luis | "Self Client" (Zoho API Console) |
| `ZOHO_INGEST_CLIENT_SECRET` 🔒 | Luis | idem |
| `ZOHO_INGEST_REFRESH_TOKEN` 🔒 | Luis | gerado na seção 9 (deixe vazio no primeiro deploy) |
| `INGEST_ENABLED` | Luis | `false` até a simulação ser aprovada |

```bash
openssl rand -base64 48
```

As demais (conta de e-mail, pasta do WorkDrive, retenção de backup, intervalo de captura) já vêm
preenchidas no compose.

Agora clique em **Deploy** e confira nos logs a linha `Application startup complete`. Abra
`https://relatorios.europcar.com.br/saude`: deve responder `ok`.

---

## 9. Token de leitura dos e-mails (Luis, com a TI no terminal)

1. Luis abre o Zoho API Console › **Self Client** › **Generate Code**, com duração de 10 minutos, e estes escopos:

   ```
   ZohoMail.accounts.READ,ZohoMail.folders.READ,ZohoMail.messages.READ,WorkDrive.files.READ,WorkDrive.files.CREATE,WorkDrive.team.READ,WorkDrive.teamfolders.READ
   ```

2. Em seguida, no Coolify › **Terminal** do container:

   ```bash
   python -m cio zoho-token --code <código> --mostrar
   ```

3. Luis copia o token exibido para `ZOHO_INGEST_REFRESH_TOKEN` (secret) e a TI faz **Redeploy**.

O token só dá leitura de e-mails e escrita na pasta do WorkDrive. Ele não expira sozinho; se vazar,
Luis o revoga no Zoho API Console e gera outro.

---

## 10. Primeiro administrador e diagnóstico

No Coolify › Terminal do container:

```bash
python -m cio add-user luis.miguel@europcar.com.br admin --group diretoria
```

```bash
python -m cio diagnostico
```

O diagnóstico confere, sem exibir segredos: banco, versão do `pg_dump` frente ao servidor, pastas graváveis,
backup em volume separado e credenciais configuradas. **Tudo deve vir `true` ou `ok`.**

Depois, Luis testa o botão **"Entrar com Zoho"** em `https://relatorios.europcar.com.br`.

---

## 11. Simulação e ativação da captura

Simulação, que lê os e-mails sem gravar nada:

```bash
python -m cio ingest --dry-run --since 2026-10-01
```

Luis revisa o resultado. Com a aprovação, a carga inicial:

```bash
python -m cio ingest --since 2026-10-01 --batch 50
```

Por fim, `INGEST_ENABLED=true` e **Redeploy**. A partir daí o portal busca novos relatórios a cada 10 minutos.

---

## 12. Backup (o portal faz; a TI leva para fora)

O portal faz sozinho, sem nenhuma configuração extra:

| Quando | O quê |
|---|---|
| Todo dia, 02:00 | Conjunto com o banco (`pg_dump`, formato custom) e os arquivos originais, com SHA-256 de cada um, no volume `cio-backups`. |
| Domingo, 03:30 | Teste de restauração automático: confere os SHA-256, lê o dump e, se `RESTORE_TEST_DATABASE_URL` existir, restaura de verdade no banco de teste e compara as contagens. |
| Sempre | Retenção de 30 diários + 12 mensais. Resultado visível em Administração (linha "Backup") e em Auditoria. |

**A TI precisa:**
1. **Copiar o volume `cio-backups` para fora do servidor** diariamente, depois das 02:30 (outro disco, NAS,
   storage ou a rotina de backup que a TI já usa). Um backup no mesmo disco não protege contra perda do servidor.
   Para localizar o volume no host:

   ```bash
   docker volume ls | grep cio-backups
   ```

   ```bash
   docker volume inspect <nome-do-volume> --format '{{ .Mountpoint }}'
   ```

2. Manter, se já existir, o backup próprio do PostgreSQL feito pelo Coolify ou pela TI (camada extra).
3. Rodar um backup manual antes de cada atualização (botão **Backup + teste de restauração** no Admin, ou):

   ```bash
   python -m cio backup
   ```

   O resultado deve conter `"ok": true`.

---

## 13. Operação do dia a dia

| O que olhar | Onde |
|---|---|
| Portal no ar | `https://relatorios.europcar.com.br/saude` responde `ok` (o Coolify também usa essa verificação). |
| Captura funcionando | Topo das páginas: "Atualizado em …". O portal mostra um aviso se a captura falhar seguidas vezes. |
| Erros de captura | Administração › painel (erros com "Tentar de novo"). |
| Backup | Administração › linha "Backup" e Auditoria. |
| Logs | Coolify › recurso › Logs. Os logs não contêm senhas nem tokens. |

**Parar só a captura** (o portal continua consultável): `INGEST_ENABLED=false` e Redeploy.

---

## 14. Atualização e rollback

**Atualizar** (quando houver uma nova versão, por exemplo 0.9.2):
1. Backup manual (seção 12, item 3).
2. Construir a nova imagem com a nova tag:

   ```bash
   docker build --build-arg CIO_VERSION=0.9.2 -t cio-portal:0.9.2 .
   ```

3. No compose do Coolify, troque `image: cio-portal:0.9.2` por `cio-portal:0.9.2` e faça Redeploy.
4. Rode `python -m cio diagnostico`.

**Voltar de versão:** troque a tag de volta para a anterior e faça Redeploy. A imagem antiga continua no servidor:

```bash
docker images cio-portal
```

As migrações de banco dentro da linha 0.9.x só acrescentam tabelas e colunas, então a versão anterior continua
funcionando sobre o banco migrado.

---

## 15. Recuperação (restaurar um backup)

Com o portal **parado** (Coolify › Stop), no terminal do container:

1. Escolha o conjunto:

   ```bash
   ls /backups
   ```

2. Banco:

   ```bash
   pg_restore --clean --if-exists --no-owner --no-privileges --dbname="$DATABASE_URL" /backups/cio-AAAAMMDD-HHMMSS.db.dump
   ```

3. Arquivos (confere o SHA-256 de cada um):

   ```bash
   python -m cio restaurar-arquivos --conjunto AAAAMMDD-HHMMSS --destino /tmp/restauro
   ```

   Copie o conteúdo de `/tmp/restauro/originals` para `/data/originals`.
4. Inicie o portal e confira:

   ```bash
   python -m cio diagnostico
   ```

   ```bash
   python -m cio verificar-backup
   ```

**Servidor perdido por completo:** novo servidor com Coolify → seções 5 a 8 → recuperar os volumes a partir da
cópia externa → seção 15.

---

## 16. Segurança: o que a TI deve saber

- **Segredos** (senha do banco, `CIO_SECRET_KEY`, client secrets e refresh token) ficam só nas variáveis
  *secret* do Coolify. Nunca em e-mail, chat, planilha ou no arquivo de compose.
- **Trocar segredos:** a senha do banco com `ALTER ROLE cio_app PASSWORD '...'` e depois atualizar
  `DATABASE_URL`. Trocar o `CIO_SECRET_KEY` desconecta todos os usuários, que só precisam entrar de novo.
- O portal só aceita e-mails `@europcar.com.br` e só publica relatórios com **DMARC aprovado** pelo Zoho.
- Arquivos originais ficam somente leitura; o conteúdo dos e-mails é exibido num quadro isolado, sem scripts.
- O portal não usa links públicos do WorkDrive e não envia dados para serviços de terceiros além do Zoho.
- Sessões expiram após 8 horas sem uso ou 7 dias no máximo.

---

## 17. Problemas comuns

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| Certificado não é emitido | DNS ainda não propagou, porta 80 fechada ou CAA restritivo | `nslookup`; liberar a porta 80; seção 4 |
| `Application startup complete` não aparece | `DATABASE_URL` errada ou rede do Coolify | conferir variável e "Connect to Predefined Network" |
| Erro `extension "unaccent" does not exist` | script SQL não rodou no banco certo | rodar a parte `\connect cio_portal` do script |
| "Entrar com Zoho" volta com erro | client ID/secret ou endereço de retorno | o endereço de retorno no Zoho deve ser exatamente `https://relatorios.europcar.com.br/auth/callback` |
| "Atualizado em" parado | token de ingestão inválido ou saída para o Zoho bloqueada | Administração › erros; seção 3.3; refazer a seção 9 |
| Diagnóstico acusa `pg_dump` antigo | servidor PostgreSQL mais novo que 17 | avisar Luis: a imagem precisa do cliente da mesma versão |
| Envio de arquivo grande falha | limite do proxy | seção 7, item 4 |

---

## 18. Checklist de entrega

- [ ] IP público informado e DNS `relatorios.europcar.com.br` ativo
- [ ] Portas 80/443 de entrada e saídas da seção 3.3 liberadas
- [ ] Banco `cio_portal`, usuário `cio_app` e (recomendado) `cio_restore_test` criados
- [ ] Imagem `cio-portal:0.9.2` construída e recurso criado no Coolify
- [ ] Variáveis preenchidas como secret; deploy com `/saude` = `ok` e HTTPS válido
- [ ] `python -m cio diagnostico` todo `true`/`ok`
- [ ] Primeiro login de Luis pelo "Entrar com Zoho"
- [ ] `python -m cio backup` com `"ok": true` e restauração completa
- [ ] Cópia diária do volume `cio-backups` para fora do servidor configurada
- [ ] Simulação revisada por Luis; só então `INGEST_ENABLED=true`

**Arquivos de apoio no pacote:** `deploy/postgres_setup.sql`, `deploy/coolify/docker-compose.yml`,
`deploy/COOLIFY.md` (versão resumida deste guia), `DNS_REQUEST_EN.md`, `Dockerfile`, `.env.example`.
