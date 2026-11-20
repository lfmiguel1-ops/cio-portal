-- Executar UMA vez, pela TI, com um usuário administrador do PostgreSQL (ex.: o do Coolify).
-- Troque a senha abaixo por uma forte; ela vai depois só no DATABASE_URL do portal (Coolify › Environment).
-- psql: \set senha 'TROQUE-ESTA-SENHA'   (ou edite a linha CREATE ROLE)

CREATE ROLE cio_app LOGIN PASSWORD 'TROQUE-ESTA-SENHA' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE cio_portal OWNER cio_app ENCODING 'UTF8' TEMPLATE template0;
REVOKE ALL ON DATABASE cio_portal FROM PUBLIC;

\connect cio_portal
-- Extensão padrão do PostgreSQL usada para a busca ignorar acentos ("caucao" encontra "caução").
CREATE EXTENSION IF NOT EXISTS unaccent;

-- ---------------------------------------------------------------------------------------------
-- OPCIONAL (recomendado): banco vazio usado SÓ pelo teste semanal de restauração do backup.
-- Depois, no Coolify: RESTORE_TEST_DATABASE_URL=postgresql://cio_app:SENHA@<host-interno>:5432/cio_restore_test
\connect postgres
CREATE DATABASE cio_restore_test OWNER cio_app ENCODING 'UTF8' TEMPLATE template0;
REVOKE ALL ON DATABASE cio_restore_test FROM PUBLIC;
\connect cio_restore_test
CREATE EXTENSION IF NOT EXISTS unaccent;
