-- Rôle PostgreSQL en lecture seule du service FastAPI : anitche_fastapi_ro.
--
-- Idempotent : peut être relancé à chaque déploiement (crée le rôle s'il
-- manque, remet ses attributs et son mot de passe, redonne les droits).
-- À exécuter avec un compte administrateur, sur la base de l'application :
--
--   infra/scripts/create_fastapi_readonly_role.sh          (Docker)
--   FASTAPI_DB_PASSWORD=... psql -v ON_ERROR_STOP=1 -d anitche -f infra/postgres/fastapi_readonly.sql
--                                                          (Postgres managé)
--
-- Le mot de passe est lu dans la variable d'environnement FASTAPI_DB_PASSWORD
-- (\getenv, psql 15 ou plus), jamais écrit dans ce fichier ni passé en
-- argument de commande.
--
-- Moindre privilège : CONNECT sur la base et USAGE sur le schéma public,
-- AUCUN droit sur les tables à ce stade. Chaque module FastAPI ajoutera
-- ci-dessous, table par table, les GRANT SELECT dont il a besoin (jamais de
-- GRANT sur tout le schéma ni d'ALTER DEFAULT PRIVILEGES) : les tables
-- sensibles (KYC, paiements...) restent illisibles.
--
-- La lecture seule côté rôle (default_transaction_read_only) et côté
-- connexion (app/core/resources.py) protège contre une erreur de code ; la
-- vraie barrière reste l'absence de droits d'écriture.

\set ON_ERROR_STOP on
\getenv fastapi_password FASTAPI_DB_PASSWORD

\if :{?fastapi_password}
\else
    \set fastapi_password ''
\endif
SELECT length(:'fastapi_password') >= 12 AS password_ok \gset
\if :password_ok
\else
    DO $$ BEGIN RAISE EXCEPTION 'FASTAPI_DB_PASSWORD doit être défini (12 caractères au moins).'; END $$;
\endif

SELECT 'CREATE ROLE anitche_fastapi_ro'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anitche_fastapi_ro') \gexec

SELECT format(
    'ALTER ROLE anitche_fastapi_ro WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
    'NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'fastapi_password'
) \gexec

ALTER ROLE anitche_fastapi_ro SET default_transaction_read_only = on;
ALTER ROLE anitche_fastapi_ro SET statement_timeout = '3s';

SELECT format('GRANT CONNECT ON DATABASE %I TO anitche_fastapi_ro', current_database()) \gexec
GRANT USAGE ON SCHEMA public TO anitche_fastapi_ro;

-- Tables lues par FastAPI, ajoutées module par module :
-- (module 1, suivi GPS) GRANT SELECT ON TABLE ... TO anitche_fastapi_ro;
