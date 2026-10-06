# Sauvegardes et restauration

Sauvegarde chiffrée, hors du serveur, de la base PostgreSQL et des médias (dossiers KYC compris), avec restauration vérifiée. Outil : [restic](https://restic.readthedocs.io/) 0.19.1, dans un conteneur à usage unique.

## 1. En bref

| Sujet | Règle |
|---|---|
| Ce qui est sauvegardé | La base entière (`pg_dump`, format custom) et le volume `anitche_media` (images, logos, pièces jointes du support, preuves des retours, **pièces d'identité et selfies KYC**) |
| Où | Un dépôt restic sur un stockage compatible S3 hors du serveur (Cloudflare R2, Backblaze B2, Wasabi…). Jamais sur le disque du serveur |
| Quand | Chaque nuit à **3 h UTC** (cron) ; contrôle approfondi le **dimanche** |
| Rétention | 7 quotidiennes, 4 hebdomadaires, 6 mensuelles, séparément pour la base et les médias |
| Chiffrement | Avant l'envoi, sur le serveur (AES-256, clé dérivée de `BACKUP_PASSWORD`). Le stockage ne voit ni contenu, ni noms de fichiers, ni métadonnées |
| Échec visible | Journal `infra/backups/backup.log`, code de sortie, appel de surveillance « heartbeat » après chaque réussite |
| Restauration | `infra/scripts/restore.sh`, dans une base et un dossier vides seulement ; exercice **mensuel** sur une autre machine |

Pas sauvegardés, volontairement : `anitche_static` (recréé par `collectstatic`), Redis (cache, file Celery, compteurs de limite de débit ; les plannings de Celery beat sont en base), les certificats (réémis par `infra/scripts/init-lets-encrypt.sh`) et `infra/.env`, reconstruit depuis le gestionnaire de secrets (§ 7) : il contient les clés qui ne doivent jamais voyager avec les sauvegardes.

## 2. Fonctionnement

```text
cron (3 h UTC) → infra/scripts/backup.sh
                   charge infra/.env, vérifie les variables (noms seulement)
                   docker compose … build backup
                   docker compose … run --rm backup sauvegarder
                     1. dépôt joignable (jamais créé automatiquement)
                     2. base   : pg_dump → restic, par un tube (aucun fichier en clair)
                     3. médias : anitche_media, monté en lecture seule
                     4. rétention : forget --prune
                     5. contrôle : check (le dimanche : check --read-data-subset)
                   journal, code de sortie, appel de surveillance si tout est OK
```

| Fichier | Rôle |
|---|---|
| `infra/backup/Dockerfile` | Image `postgres:16-alpine` (même version que `db`) + binaire restic épinglé (version et empreinte) |
| `infra/backup/entrypoint.sh` | Sous-commandes `init`, `sauvegarder`, `instantanes`, `restaurer` |
| `infra/scripts/backup.sh` | Script de l'hôte appelé par cron |
| `infra/scripts/restore.sh` | Script de l'hôte pour lister, s'exercer et reprendre après sinistre |
| `infra/docker-compose.prod.yml`, service `backup` | Profil `backup` : jamais démarré par `up -d`. Reçoit seulement les variables de la base et de la sauvegarde (ni `SECRET_KEY`, ni `FIELD_ENCRYPTION_KEYS`, ni les clés CinetPay) ; aucun autre service ne reçoit les variables `BACKUP_*` |

Points de conception :

- **Pas d'archive tronquée** : si `pg_dump` échoue (mot de passe, base arrêtée), restic ne crée aucun instantané de la base et la sauvegarde sort en erreur.
- **Base puis médias** : la copie des médias, plus tardive, contient tous les fichiers référencés par le dump, sauf ceux supprimés entre les deux étapes (resoumission d'un dossier KYC, fenêtre de quelques secondes). La restauration compte ces fichiers manquants (§ 6.4).
- **Hôte fixe `anitche`** : le nom d'un conteneur `run --rm` change à chaque exécution ; restic regroupe les instantanés par hôte et chemin pour la rétention.
- **Rétention par périodes, pas par âge** : si les sauvegardes s'arrêtent, les dernières ne sont pas effacées au bout de N jours.

Abréviation utilisée ci-dessous, depuis la racine du dépôt :

```bash
compose() { docker compose -f infra/docker-compose.prod.yml --env-file infra/.env --profile backup "$@"; }
```

## 3. Mise en place (une fois, sur le serveur)

1. **Bucket** dédié chez le fournisseur S3, et une clé d'accès limitée à ce seul bucket : lecture, écriture, liste et suppression d'objets (la suppression sert à la rétention).
2. **Mot de passe du dépôt** : `openssl rand -base64 32`. L'enregistrer dans le gestionnaire de secrets **et** remettre la copie hors ligne au chef de projet (§ 7) **avant** la première sauvegarde.
3. **`infra/.env`** : remplir `BACKUP_REPOSITORY`, `BACKUP_PASSWORD`, `BACKUP_S3_ACCESS_KEY_ID`, `BACKUP_S3_SECRET_ACCESS_KEY`, et selon le fournisseur `BACKUP_S3_REGION` (`auto` pour R2). Modèle commenté : `infra/.env.example`. Contrôle :
   ```bash
   set -a; . infra/.env; set +a; bash infra/scripts/check_prod_env.sh
   ```
   Il refuse un dépôt qui n'est pas `s3:https://<endpoint>/<bucket>` (chemin local compris), un mot de passe de moins de 32 caractères et une adresse de surveillance qui n'est pas en `https://`. `deploy.sh` le lance à chaque déploiement : pas de déploiement sans sauvegarde configurée.
4. **Créer le dépôt** (une seule fois ; refusé s'il existe déjà) :
   ```bash
   compose run --rm --no-deps backup init
   ```
5. **Première sauvegarde**, à la main : `infra/scripts/backup.sh`, puis `infra/scripts/restore.sh --lister` (un instantané `db` et un `media`).
6. **Horloge en UTC** : `timedatectl` doit afficher `Time zone: Etc/UTC` (sinon `sudo timedatectl set-timezone Etc/UTC`). 3 h UTC = 3 h à Abidjan (UTC+0 toute l'année). Puis `crontab -e`, pour l'utilisateur qui lance Docker, avec le chemin réel du dépôt :
   ```text
   # Sauvegarde ANITCHE : chaque nuit à 3 h UTC (horloge du serveur en UTC)
   0 3 * * * /srv/anitche/infra/scripts/backup.sh > /dev/null 2>&1
   ```
   Le script écrit tout dans son journal ; la sortie de cron est donc ignorée.
7. **Surveillance** : créer un contrôle « heartbeat » (healthchecks.io, Uptime Kuma en mode push, Better Stack, Cronitor), période 24 h, tolérance 2 h, alertes vers l'administrateur et le chef de projet. Mettre son adresse `https://` dans `BACKUP_PING_URL`.
8. **Premier exercice de restauration** (§ 6.2) dans la foulée.

## 4. Journal et échecs

`infra/backups/backup.log` (dossier en 700, fichier en 600, ignoré par Git), une ligne horodatée en UTC par étape :

```text
2026-10-04T03:00:00Z sauvegarde : début
2026-10-04T03:00:05Z dépôt : OK
2026-10-04T03:00:41Z base : OK
2026-10-04T03:02:10Z médias : OK
2026-10-04T03:02:30Z rétention : OK
2026-10-04T03:02:45Z contrôle : OK
2026-10-04T03:02:46Z sauvegarde : OK (166 s)
2026-10-04T03:02:47Z surveillance : appel envoyé
```

| Code de sortie | Sens |
|---|---|
| 0 | Sauvegarde complète réussie, appel de surveillance envoyé (s'il est configuré) |
| 1 | Échec (variable absente, `pg_dump` en erreur, dépôt injoignable, rétention ou contrôle en erreur) ; l'étape fautive est au journal |
| 2 | Option inconnue |
| 3 | Un fichier média n'a pas pu être lu : instantané incomplet, compté comme un échec |
| 10, 11, 12 | Dépôt introuvable, dépôt verrouillé, mauvais mot de passe |

- L'appel de surveillance part **seulement après une réussite complète**. Le service alerte donc aussi quand cron ne tourne pas ou que le serveur est éteint, ce qu'un message d'échec ne signalerait jamais. Un appel qui échoue est noté en avertissement sans changer le code de sortie.
- Une nuit manquée (serveur éteint à 3 h) n'est pas rattrapée : l'alerte de surveillance le signale ; relancer `infra/scripts/backup.sh` à la main.
- Le journal ne contient ni mot de passe, ni clé, ni l'adresse de surveillance.
- **Verrou** : deux exécutions simultanées font échouer la seconde. Un verrou laissé par un arrêt brutal se lève avec `compose run --rm --no-deps --entrypoint restic backup unlock` (seulement si aucune sauvegarde ne tourne).

## 5. Contrôle d'intégrité

- **Chaque nuit** : `restic check` vérifie la structure du dépôt (index, présence des paquets, arbres des instantanés), sans relire les données.
- **Chaque dimanche (UTC)** : `restic check --read-data-subset=<n>/8` télécharge et vérifie une part des données (environ un huitième), une part différente chaque semaine : tout le dépôt est relu en 8 semaines. À la demande : `infra/scripts/backup.sh --controle-approfondi`.
- **Relecture complète**, après un incident chez le fournisseur par exemple : `compose run --rm --no-deps --entrypoint restic backup check --read-data`.

Ces relectures téléchargent des données : gratuit chez R2, dans les limites de sortie gratuites chez B2 et Wasabi.

## 6. Restauration

`restore.sh` n'écrase jamais de données : la base cible doit être absente ou vide, le dossier des médias vide ; sinon refus, sortie 1, rien d'écrit. Le service `db` doit tourner.

### 6.1 Lister les instantanés

```bash
infra/scripts/restore.sh --lister
```

Par défaut, la restauration prend le dernier instantané `db` et le dernier `media`. Pour une autre nuit : `--instantane-base <ID> --instantane-medias <ID>`, deux identifiants **de la même nuit** (quelques secondes d'écart, base d'abord).

### 6.2 Exercice mensuel (sur une autre machine)

Une fois par mois, sur un poste avec Docker, **jamais sur le serveur de production** (disque et mémoire). Cela prouve que la sauvegarde se restaure **et** que le mot de passe gardé hors du serveur est le bon.

1. Clone du dépôt dans un dossier dédié (pas le dossier de développement).
2. `infra/.env` limité à :
   ```text
   COMPOSE_PROJECT_NAME=anitche-exercice
   DB_NAME=anitche
   DB_USER=anitche_exercice
   DB_PASSWORD=<aléatoire>
   BACKUP_REPOSITORY=<gestionnaire de secrets>
   BACKUP_PASSWORD=<copie hors ligne du chef de projet>
   BACKUP_S3_ACCESS_KEY_ID=<gestionnaire de secrets>
   BACKUP_S3_SECRET_ACCESS_KEY=<gestionnaire de secrets>
   BACKUP_S3_REGION=<si besoin>
   ```
   `COMPOSE_PROJECT_NAME` isole les conteneurs et volumes de l'exercice de ceux du développement (même nom de projet, `infra`, sinon).
3. `docker compose -f infra/docker-compose.prod.yml --env-file infra/.env up -d --wait db` (les avertissements « variable is not set » des autres services sont sans effet).
4. `infra/scripts/restore.sh` : base jetable `anitche_restauration_<AAAAMMJJ>`, médias dans un dossier temporaire du conteneur, puis les vérifications du § 6.4. Les nombres de lignes de la « base vivante » de cette machine sont sans objet.
5. Noter la date, les instantanés, la durée et le résultat dans le journal des exercices (§ 10).
6. Nettoyage : `docker compose -p anitche-exercice -f infra/docker-compose.prod.yml down -v` (base et volumes **de l'exercice** seulement ; ne jamais lancer `down -v` sur le serveur de production).

### 6.3 Reprise après sinistre (serveur perdu)

1. Nouveau serveur : Docker, clone du dépôt, horloge en UTC.
2. `infra/.env` reconstruit depuis le gestionnaire de secrets, droits 600, avec **toutes** les clés `FIELD_ENCRYPTION_KEYS` encore valables pour la date de la sauvegarde (registre, § 7.2).
3. `docker compose -f infra/docker-compose.prod.yml --env-file infra/.env up -d --wait db`
4. `infra/scripts/restore.sh --lister`, puis `infra/scripts/restore.sh --reprise` : base `DB_NAME` (vide à la création du serveur) et volume `anitche_media` (vide), mêmes vérifications. Toutes doivent être OK.
5. `infra/scripts/deploy.sh` : construction, migrations (rien à appliquer pour la même version), rôle FastAPI (ses droits sont recréés : la restauration ne reprend aucun `GRANT`), démarrage. Les certificats, non sauvegardés, se réémettent avec `infra/scripts/init-lets-encrypt.sh` (cas « perte totale du volume de certificats » de son en-tête).
6. Contrôles : connexion, catalogue, un dossier KYC affiché dans l'administration, puis `docker compose -f infra/docker-compose.prod.yml --env-file infra/.env exec backend-django python manage.py rechiffrer_donnees_sensibles --simulation` : **0 illisible**.
7. Réinstaller cron et la surveillance (§ 3), lancer une sauvegarde.
8. Noter la durée totale, de la commande 3 au contrôle 6, dans le journal des exercices.

**Si une restauration s'arrête en cours de route** (réseau, instantané erroné), la base a pu être restaurée sans les médias : avant de relancer, vider les cibles. Exercice : `DROP DATABASE` de la base jetable. Reprise, sur le **nouveau** serveur seulement : `DROP DATABASE` puis `CREATE DATABASE` de `DB_NAME`, et volume des médias recréé vide.

### 6.4 Ce que vérifie la restauration

| Vérification | Échec si |
|---|---|
| `pg_restore` en une seule transaction | Une erreur : la base cible reste vide |
| `restic restore --verify` des médias | Un fichier restauré diffère du dépôt |
| Extensions `unaccent` et `pg_trgm` | L'une manque |
| Nombre de lignes de 6 tables (utilisateurs, boutiques, produits, commandes, paiements, dossiers KYC), à côté de ceux de la base vivante quand elle est joignable | Une table illisible (les écarts avec la base vivante sont normaux : elle a changé depuis la sauvegarde) |
| Fichiers référencés par la base restaurée (KYC recto, verso et selfie, images des produits et catégories, logos et bannières, pièces jointes du support, preuves des retours) présents dans les médias restaurés | Au moins un manquant (les 20 premiers sont listés) |
| Durée totale | Affichée |

Tout nouveau champ `FileField` ou `ImageField` s'ajoute à `REQUETE_FICHIERS` dans `infra/backup/entrypoint.sh`, sinon ses fichiers ne sont pas vérifiés.

## 7. Garde des clés

| Secret | Où il vit | Jamais | Combien de temps |
|---|---|---|---|
| `BACKUP_PASSWORD` | `infra/.env` du serveur (600) ; gestionnaire de secrets ; **copie hors ligne** (papier ou clé USB chiffrée, au coffre) **détenue par le chef de projet** | Dans le bucket ou le compte du fournisseur de stockage, dans Git, dans les sauvegardes | Tant qu'une sauvegarde existe. **Perdu = toutes les sauvegardes illisibles** |
| `BACKUP_S3_*` | `infra/.env` ; gestionnaire de secrets (pour restaurer depuis une autre machine) | Dans Git | Tant que le bucket existe |
| `FIELD_ENCRYPTION_KEYS` | `infra/.env` ; gestionnaire de secrets ([`MODULE_UTILISATEURS.md`](./MODULE_UTILISATEURS.md) § 4 bis) | Dans les sauvegardes (`infra/.env` n'est pas sauvegardé), dans le bucket, dans Git | Registre ci-dessous |

Deux personnes connaissent ou détiennent `BACKUP_PASSWORD` : l'administrateur du serveur et le chef de projet. L'exercice mensuel se fait avec la copie hors ligne.

### 7.1 Ce que voit le stockage

Uniquement des paquets chiffrés nommés par leur empreinte. restic garde dans le dépôt sa clé maîtresse **chiffrée** par une clé dérivée de `BACKUP_PASSWORD` (scrypt) ; sans le mot de passe, elle est inutilisable. Le dump passe de `pg_dump` à restic par un tube : aucun fichier en clair n'est écrit sur un disque.

### 7.2 Registre des clés `FIELD_ENCRYPTION_KEYS`

Les numéros mobile money et les numéros de destinataire des reversements sont chiffrés en base avec ces clés. Une sauvegarde restaurée n'est lisible qu'avec les clés en service à sa date. Le gestionnaire de secrets tient donc un **registre**, une ligne par clé Fernet :

| Clé | Activée le | Retirée le | À conserver jusqu'au |
|---|---|---|---|
| (nom de l'entrée dans le gestionnaire, jamais la valeur) | date | date de l'étape 6 de la rotation | date de retrait **+ 7 mois** |

7 mois : la plus ancienne sauvegarde gardée a 6 mois (rétention mensuelle), plus une marge. Aucune clé n'est détruite avant cette date. Après une restauration, `rechiffrer_donnees_sensibles --simulation` doit indiquer **0 illisible**. Les valeurs ne figurent jamais dans la documentation, seulement la règle et l'emplacement.

### 7.3 Changer `BACKUP_PASSWORD`

restic peut avoir plusieurs mots de passe pour un même dépôt ; les données ne sont pas rechiffrées.

1. `compose run --rm --no-deps --entrypoint restic backup key add` (saisie du nouveau mot de passe), puis `… backup key list`.
2. Mettre à jour `infra/.env`, le gestionnaire de secrets et la copie hors ligne.
3. Vérifier avec le nouveau : `infra/scripts/restore.sh --lister`.
4. `compose run --rm --no-deps --entrypoint restic backup key remove <ID de l'ancienne clé>`.

Si le mot de passe **et** l'accès au bucket ont fuité ensemble, changer de mot de passe ne suffit pas (l'attaquant a pu copier la clé maîtresse) : nouveau bucket, nouveau dépôt, nouveau mot de passe ; l'ancien dépôt est supprimé quand le nouveau couvre la période de rétention.

## 8. Maintenance

- **Mise à jour de restic** : nouvelle étiquette et nouvelle empreinte dans `infra/backup/Dockerfile` (commande en commentaire), puis une sauvegarde et un exercice de restauration.
- **Nouvelle version majeure de PostgreSQL** : changer l'image de `db` et celle de `infra/backup/Dockerfile` ensemble. Un `pg_dump` plus ancien que le serveur refuse de le sauvegarder (échec visible au journal).
- **Taille du journal** : une vingtaine de lignes par nuit ; `logrotate` si besoin.
- **Wasabi** facture 90 jours au minimum par objet : les données retirées plus tôt par la rétention restent facturées (coût, pas risque). R2 et B2 n'ont pas cette règle.
- **Mémoire** : mesurée sur les données de développement (base de 14 Mo, 522 médias) : 54 Mo au plus pour une sauvegarde, 75 Mo avec une rétention qui supprime 47 instantanés, 45 Mo pour une restauration. À mesurer de nouveau en production ; si le pic gêne, `GOGC` plus bas ou `prune` le dimanche seulement.

## 9. Limites connues

| Limite | Mesure |
|---|---|
| Un attaquant maître du serveur peut lire toutes les sauvegardes (le mot de passe y est) | Acceptée : il lit déjà la base vivante et possède `FIELD_ENCRYPTION_KEYS` |
| Il peut aussi **supprimer** les sauvegardes (la clé S3 a le droit de supprimer, nécessaire à la rétention) | À traiter : versionnement ou verrouillage d'objets du bucket, clés aux droits séparés, ou seconde copie récupérée par une autre machine. Dépend du fournisseur |
| Fenêtre base puis médias (resoumission KYC pendant la sauvegarde) | Rare ; comptée par la restauration (§ 6.4) |
| La restauration crée la base et les extensions avec `DB_USER`, aujourd'hui superutilisateur | Si ce rôle perd ses droits, il lui faudra `CREATEDB`, et `unaccent` / `pg_trgm` devront être créées par un superutilisateur |
| Pas de sauvegarde automatique avant les migrations de `deploy.sh` | À ajouter dans `deploy.sh` ; `backup.sh` est appelable tel quel |

## 10. Journal des exercices

| Date | Machine | Instantanés (base / médias) | Durée | Résultat | Par |
|---|---|---|---|---|---|
| | | | | | |
