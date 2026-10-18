# Déploiement en production

Mode d'emploi du serveur de production : premier serveur, déploiement courant, retour arrière, ouverture de l'administration Django. Le détail des étapes de `infra/scripts/deploy.sh` est dans [`GUIDE_STRUCTURE_ANITCHE.md`](./GUIDE_STRUCTURE_ANITCHE.md) § 5, les sauvegardes dans [`SAUVEGARDES.md`](./SAUVEGARDES.md).

Abréviation utilisée ci-dessous, depuis la racine du dépôt sur le serveur :

```bash
compose() { docker compose -f infra/docker-compose.prod.yml --env-file infra/.env "$@"; }
```

## 1. Domaines et nginx

Aucun nom de domaine n'est écrit dans `infra/nginx/nginx.conf` : c'est un modèle, rendu au démarrage du conteneur nginx avec les variables de `infra/.env`.

| Variable | Défaut | Rôle |
|---|---|---|
| `DOMAIN` | aucun (obligatoire) | Domaine du site (`anitche.com`), servi en HTTPS avec `www.` ; nom du certificat |
| `DOMAIN_VENDEUR`, `DOMAIN_LIVREUR`, `DOMAIN_ADMIN` | `vendeur.`, `livreur.`, `admin.` de `DOMAIN` | Sous-domaines des portails, dans le même certificat. Servis pour l'instant en HTTP seulement (challenge ACME, redirection vers HTTPS) |
| `ADMIN_ALLOWED_CIDRS` | vide | Adresses autorisées sur `/admin/` (Django). **Vide : 403 pour tous** (§ 5) |
| `NGINX_RELOAD_INTERVAL` | `6h` | Rechargement périodique de nginx, qui lui fait servir le certificat renouvelé par certbot |

Un staging sur un autre domaine ne change que `DOMAIN` et les variables qui en dépendent (`ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `BACKEND_BASE_URL`, `FRONTEND_BASE_URL`, `PUBLIC_BASE_URL`, `MEDIA_BASE_URL`). `DEFAULT_FROM_EMAIL` reste en `@anitche.com`, domaine d'envoi imposé par `prod.py`. La CI fait tourner toute la pile sur `staging.exemple.test`.

Comportement de nginx :

- Host ou SNI inconnus, accès par l'adresse IP du serveur : connexion fermée sans réponse (`return 444`, poignée de main TLS refusée). Aucun scan n'atteint Django et le certificat n'est montré qu'aux noms qu'il couvre.
- Valeur invalide (nom d'hôte, adresse de `ADMIN_ALLOWED_CIDRS`, préfixe `/0`, intervalle) : nginx refuse de démarrer (`infra/nginx/entrypoint.d/40-anitche-config.sh`) ; `check_prod_env.sh` les refuse avant le déploiement.
- Backends résolus à chaque requête (`resolver` de Docker) : nginx démarre sans eux et suit un conteneur recréé, sans rechargement.

## 2. Premier serveur

Dans cet ordre : chaque étape a besoin de la précédente.

1. **DNS** : enregistrements A (et AAAA si le serveur a de l'IPv6) vers le serveur pour `DOMAIN`, `www.DOMAIN` et les trois sous-domaines des portails. Chez Cloudflare, les laisser en « DNS only » (nuage gris) jusqu'à l'étape 7 : le certificat factice de l'étape 6 serait refusé en mode Full (strict). Vérifier : `dig +short <nom>` donne l'adresse du serveur pour chacun des cinq noms.
2. **Pare-feu** : entrées 22 (SSH), 80 et 443 seulement (`ufw default deny incoming`, `ufw allow 22,80,443/tcp`). Seul nginx publie des ports ; db, Redis, Django et FastAPI ne sont joignables que sur le réseau Docker. Docker contourne `ufw` pour les ports publiés : ne jamais ajouter de `ports:` à un autre service.
3. **Docker** et le plugin compose (dépôt officiel de Docker), horloge en UTC (`timedatectl set-timezone Etc/UTC`), utilisateur non root membre du groupe `docker`.
4. **Code** : `git clone` dans `/srv/anitche`, branche `main`.
5. **`infra/.env`** depuis `infra/.env.example`, valeurs tirées du gestionnaire de secrets, `chmod 600 infra/.env`. Contrôle : `set -a; . infra/.env; set +a; bash infra/scripts/check_prod_env.sh`.
6. **Certificat** : `./infra/scripts/init-lets-encrypt.sh` (une fois). Il démarre nginx avec un certificat factice, obtient par webroot un certificat unique pour les cinq noms, puis recharge nginx. Échec typique : un nom qui ne pointe pas encore vers le serveur, ou le port 80 fermé.
7. **Cloudflare** : nuage orange sur les cinq noms, SSL/TLS en **Full (strict)**.
8. **Dépôt de sauvegarde** : [`SAUVEGARDES.md`](./SAUVEGARDES.md) § 3, étapes 1 à 4 (bucket, mot de passe, `backup init`). Obligatoire **avant** le premier déploiement : `deploy.sh` sauvegarde avant de migrer et s'arrête si la sauvegarde échoue.
9. **Déploiement** : `./infra/scripts/deploy.sh`. La première fois, toutes les migrations sont en attente : une sauvegarde (de la base vide) précède leur application.
10. **Cron de sauvegarde et surveillance** : [`SAUVEGARDES.md`](./SAUVEGARDES.md) § 3, étapes 5 à 8.
11. **Contrôles** :
    - `https://DOMAIN/api/catalogue/categories/` répond 200 ;
    - `https://DOMAIN/admin/` répond 403 (fermé, § 5) ;
    - `compose ps` : tous les services `healthy` ;
    - depuis une autre machine, `curl -k https://<adresse IP du serveur>/` échoue (poignée de main refusée).

## 3. Déploiement courant

```bash
cd /srv/anitche && ./infra/scripts/deploy.sh
```

Arrêt à la première erreur. Points à surveiller :

- **Migrations** : `migrer.sh` affiche le nombre de migrations en attente. S'il y en a, la sauvegarde passe d'abord (quelques minutes selon la taille des médias) ; son journal est dans `infra/backups/backup.log`. Une sauvegarde en échec arrête le déploiement **sans rien migrer** : l'ancienne version du code tourne toujours tant que l'étape `up -d` n'est pas atteinte, mais les images ont déjà été reconstruites ; corriger la sauvegarde puis relancer `deploy.sh`.
- **Jamais de migration ailleurs** : ni la commande du conteneur Django, ni un `compose up -d` ou `restart` manuel ne migrent. Un `compose run backend-django python manage.py migrate` à la main contournerait la sauvegarde : passer par `infra/scripts/migrer.sh`.
- **nginx** : `deploy.sh` rend de nouveau le modèle et recharge nginx après `nginx -t`. Une configuration invalide arrête le déploiement ; l'ancienne reste servie.

## 4. Retour arrière

Noter avant chaque déploiement le commit en service (`git rev-parse HEAD`) ; `migrer.sh` a créé un instantané `db` juste avant les migrations (`infra/scripts/restore.sh --lister`, à l'heure du déploiement).

**Sans migration dans la version fautive** : revenir au code précédent, sans toucher à la base.

- Voie normale : `git revert` du ou des commits sur `main` (poussé depuis un poste de développement), puis `deploy.sh`.
- En urgence, sur le serveur : `git checkout <commit précédent>`, puis `compose build` et `compose up -d` (pas `deploy.sh`, qui ferait `git pull origin main`). Revenir ensuite sur `main` une fois le correctif publié.

**Avec migrations** :

1. Migrations réversibles et sans perte (ajout de champ, de table) : avec le code **fautif** encore en place, défaire jusqu'à la dernière migration de la version précédente, application par application :
   `compose run --rm --no-deps backend-django python manage.py migrate <app> <migration précédente>` ;
   puis revenir au code précédent comme ci-dessus.
2. Migration destructive ou en échec (données transformées ou perdues) : restaurer l'instantané pris par `migrer.sh` dans une **nouvelle** base, sans toucher à la base en service :
   1. `infra/scripts/restore.sh --base anitche_retour --instantane-base <ID>` (vérifications du § 6.4 de `SAUVEGARDES.md`, toutes OK) ;
   2. `DB_NAME=anitche_retour` dans `infra/.env` ;
   3. code précédent (`git checkout <commit précédent>`), `compose build`, droits de FastAPI sur la nouvelle base (`set -a; . infra/.env; set +a; COMPOSE_FILE=infra/docker-compose.prod.yml bash infra/scripts/create_fastapi_readonly_role.sh`), `compose up -d` ;
   4. l'ancienne base reste intacte pour analyse ; la supprimer seulement après décision.

   Tout ce qui a été écrit entre l'instantané et l'arrêt (commandes, paiements) n'est pas dans la base restaurée : arrêter la pile au plus tôt, puis rapprocher les paiements avec GeniusPay.

## 5. Administration Django (`/admin/`)

Fermée par défaut : avec `ADMIN_ALLOWED_CIDRS` vide, nginx répond 403 à toute requête sous `/admin/`, sans la transmettre à Django. Les variantes de chemin (`//admin/`, `%61dmin`, `../`) sont couvertes : nginx compare l'URI normalisée.

L'adresse comparée est celle du visiteur : derrière Cloudflare, `CF-Connecting-IP`, lue **seulement** quand la connexion vient d'une plage Cloudflare (`set_real_ip_from` de `nginx.conf`). Un client qui contourne Cloudflare ne peut donc pas se faire passer pour une adresse autorisée.

### Ouvrir temporairement

1. Relever son adresse publique (celle que voit le site). Si la connexion passe en IPv6, Cloudflare transmet l'adresse IPv6 : l'ajouter aussi (`/128`, ou le `/64` du fournisseur si elle change).
2. Dans `infra/.env` : `ADMIN_ALLOWED_CIDRS=203.0.113.7/32` (plusieurs entrées séparées par des virgules ; `/0` est refusé).
3. `compose up -d nginx` : la variable a changé, nginx est recréé (coupure d'une seconde environ). Contrôle : `compose exec nginx cat /etc/nginx/anitche/admin_autorises.conf`, et `/admin/` répond depuis le poste autorisé.
4. Le travail fini : remettre `ADMIN_ALLOWED_CIDRS=` (vide), `compose up -d nginx`, vérifier le 403.

Ne jamais ouvrir à une plage partagée (Wi-Fi public, plage d'un opérateur mobile) : la liste d'adresses est la seule barrière avant l'écran de connexion.

### Piste : Cloudflare Access

Quand les adresses des administrateurs ne sont pas fixes, Cloudflare Access (Zero Trust) remplace la liste d'adresses par une identification : application « self-hosted » sur `DOMAIN/admin/*`, politique limitée aux adresses email du staff, avec un second facteur. Pour qu'elle ne soit pas contournée par un accès direct à l'adresse IP du serveur, il faut en plus, côté serveur :

- n'accepter que Cloudflare : pare-feu limité aux plages Cloudflare sur 443, ou mieux Authenticated Origin Pulls (certificat client de Cloudflare vérifié par nginx, `ssl_verify_client`) ;
- idéalement, vérifier le jeton `Cf-Access-Jwt-Assertion` (signature et audience) dans Django.

Aucun nouveau service n'est nécessaire, mais c'est un changement de configuration Cloudflare et de nginx à préparer et tester à part.

## 6. Certificat

- Le service `certbot` tente un renouvellement toutes les 12 h ; Let's Encrypt renouvelle à 30 jours de l'échéance.
- nginx garde en mémoire le certificat lu au démarrage : il se recharge toutes les `NGINX_RELOAD_INTERVAL` (6 h) et sert alors le certificat renouvelé. La CI le vérifie (nouveau certificat servi en moins de 20 s avec un intervalle de 10 s).
- Contrôle du certificat de l'**origine** (pas celui de Cloudflare) : `openssl s_client -connect <adresse IP du serveur>:443 -servername DOMAIN < /dev/null | openssl x509 -noout -dates`.
- Changement de domaine ou ajout d'un nom : mettre à jour `infra/.env`, DNS, puis relancer `infra/scripts/init-lets-encrypt.sh`.
