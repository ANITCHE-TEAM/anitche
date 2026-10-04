# infra/backup/Dockerfile
# Image de sauvegarde et de restauration (docs/SAUVEGARDES.md) : pg_dump,
# pg_restore et psql de PostgreSQL, plus restic.
#
# Même image de base que le service « db » de docker-compose.prod.yml : un
# pg_dump plus ancien que le serveur refuse de le sauvegarder. Changer la
# version de PostgreSQL ici et dans « db » en même temps.
#
# restic : binaire de l'image officielle, épinglé par version et empreinte.
# Pour monter de version : nouvelle étiquette et nouvelle empreinte
# (docker pull restic/restic:<version>, puis
# docker image inspect --format '{{index .RepoDigests 0}}' restic/restic:<version>).
FROM restic/restic:0.19.1@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510 AS restic

FROM postgres:16-alpine
COPY --from=restic --chown=root:root /usr/bin/restic /usr/local/bin/restic
COPY --chmod=755 entrypoint.sh /usr/local/bin/sauvegarde
ENTRYPOINT ["/usr/local/bin/sauvegarde"]
