# Central de Inteligência Operacional — imagem de produção.
# Versão: definida em cio/__init__.py (__version__) e usada como tag: docker build -t cio-portal:<versão> .
FROM python:3.12-slim-bookworm
ARG CIO_VERSION=0.9.2
LABEL org.opencontainers.image.title="cio-portal" org.opencontainers.image.version="${CIO_VERSION}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Cliente PostgreSQL 17 (pg_dump/pg_restore) do repositório oficial e gratuito do PostgreSQL.
# pg_dump precisa ser da mesma versão principal do servidor ou mais nova; a 17 atende servidores 13–17.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-17 \
 && apt-get purge -y curl gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 cio
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cio ./cio
RUN mkdir -p /data /backups && chown cio:cio /data /backups
USER cio
ENV CIO_DATA_DIR=/data BACKUP_DIR=/backups
VOLUME ["/data", "/backups"]
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/saude')"
CMD ["python", "-m", "cio", "serve", "--host", "0.0.0.0", "--port", "8000"]
