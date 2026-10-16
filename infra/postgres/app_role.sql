-- Rôle PostgreSQL applicatif de Django et Celery : anitche_app.
--
-- Idempotent : peut être relancé à chaque déploiement (crée le rôle s'il
-- manque, remet ses attributs et son mot de passe, lui redonne la base).
-- À exécuter avec le superutilisateur, sur la base de l'application, AVANT
-- `python manage.py migrate` :
--
--   infra/scripts/create_app_role.sh                       (Docker)
--   DB_PASSWORD=... psql -v ON_ERROR_STOP=1 -d anitche -f infra/postgres/app_role.sql
--                                                          (Postgres managé)
--
-- Le mot de passe est lu dans la variable d'environnement DB_PASSWORD
-- (\getenv, psql 15 ou plus), jamais écrit dans ce fichier ni passé en
-- argument de commande.
--
-- Ni superutilisateur, ni CREATEDB, ni CREATEROLE : une injection SQL ou un
-- shell Django n'ouvre ni COPY ... PROGRAM, ni la lecture des fichiers du
-- serveur, ni la création de rôles. Propriétaire de la base, donc du schéma
-- public (pg_database_owner, PostgreSQL 15 ou plus) : les migrations créent
-- ses tables, ses vues et les extensions « trusted » unaccent et pg_trgm
-- (catalogue 0004) sans superutilisateur.

\set ON_ERROR_STOP on
\getenv app_password DB_PASSWORD

\if :{?app_password}
\else
    \set app_password ''
\endif
SELECT length(:'app_password') >= 12 AS password_ok \gset
\if :password_ok
\else
    DO $$ BEGIN RAISE EXCEPTION 'DB_PASSWORD doit être défini (12 caractères au moins).'; END $$;
\endif

SELECT 'CREATE ROLE anitche_app'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anitche_app') \gexec

SELECT format(
    'ALTER ROLE anitche_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
    'NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'app_password'
) \gexec

SELECT format('ALTER DATABASE %I OWNER TO anitche_app', current_database()) \gexec

-- Une table, vue ou séquence du schéma public appartenant à un autre rôle
-- (base migrée par le superutilisateur, restauration sans --role) ferait
-- échouer la prochaine migration qui la modifie : arrêt explicite, avant
-- toute migration.
SELECT count(*) AS objets_etrangers, count(*) = 0 AS proprietaire_ok
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
  AND c.relowner <> 'anitche_app'::regrole \gset
\if :proprietaire_ok
\else
    SELECT format(
        'DO $$ BEGIN RAISE EXCEPTION %L; END $$',
        :objets_etrangers || ' table(s), vue(s) ou séquence(s) du schéma public appartiennent à un autre rôle '
        'que anitche_app : leur donner anitche_app pour propriétaire (ALTER ... OWNER TO anitche_app) avant de migrer.'
    ) \gexec
\endif
