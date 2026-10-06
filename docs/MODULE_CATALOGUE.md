# Module catalogue — contrat et règles

> Périmètre : backend Django, `backend-django/apps/catalogue/`.
> Document vivant : à corriger dès qu'une règle est arbitrée en réunion.

## 1. Ce sur quoi le module s'appuie, et qui s'appuie sur lui

| Élément | Où | Rôle |
|---|---|---|
| `Boutique.est_publiable` / `BoutiqueQuerySet.publiques()` | `apps/vendeurs/models.py` | Boutique ouverte, non suspendue, vendeur validé et actif. `Produit.objects.publies()` en est la traduction SQL |
| `EstVendeurValide`, `EstAdministrateur`, `BoutiqueDuVendeurNonSuspendue`, `ROLES_ADMINISTRATION` | `apps/vendeurs/permissions.py` | Accès à l'espace vendeur et à la modération. `is_staff` ne donne **aucun** pouvoir métier |
| `BoutiquePubliqueSerializer` | `apps/vendeurs/serializers.py` | Boutique sur la fiche produit publique |

Modules qui dépendent du catalogue :

| Module | Dépendance | Protection |
|---|---|---|
| `panier` | `PanierItem.variante` ; `Produit.est_achetable`, `Stock.est_en_stock()` | Une variante n'est jamais supprimée par l'API : la ligne reste, marquée indisponible |
| `commandes` | `CommandeItem.variante` (**PROTECT**) ; `Stock.decrementer()` sous `select_for_update` au checkout | Même verrou que la mise à jour de stock du vendeur (§ 5) |
| `retours` | `Stock.incrementer()` | Incrément SQL atomique |
| `passeport_qr` | `produit`, `variante` (**PROTECT**, migration passeport_qr 0006) | Certificat et historique de scans jamais effacés |
| `support` | `SupportTicket.product` (**PROTECT**, migration support 0005) | Un ticket ne perd jamais son produit |
| FastAPI (recherche, listes publiques) | Vues SQL `catalogue_produit_public`, `catalogue_categorie_publique`, `catalogue_boutique_publique` (migration 0004), en lecture seule | Test de parité (§ 11) ; le rôle FastAPI ne lit que ces vues, aucune table du catalogue (§ 2) |

Aucun appel depuis le frontend à ce jour. Collections Postman : `postman_catalogue.json` (§ 11), `postman_panier.json`, `postman_passeport_qr.json`, `postman_paiements.json`, `postman_0_setup_vendeur.json`.

## 2. Modèle

- `Categorie` : arborescence (`parent`), `est_active`, `ordre`, slug unique généré.
- `Produit` : appartient à une boutique.
  - `slug` unique (nom + suffixe aléatoire), **stable** : non modifiable par l'API, inchangé au renommage.
  - `prix_base ≥ 0` (contrainte `produit_prix_base_positif`) : prix indicatif, le prix affiché vient des variantes.
  - `est_actif` + **`desactive_par`** (`vendeur` / `administration`, vide si actif). Contrainte `produit_desactivation_coherente` : actif ⇔ `desactive_par` vide. Ne change que par `desactiver()` / `reactiver()` (UPDATE conditionnels, sûrs en cas de requêtes simultanées). Voir § 6.
- `VarianteProduit` : ce qui se vend (prix, stock). `sku` unique généré.
  - `prix > 0`, `prix_promo` nul ou `0 < prix_promo < prix`, `poids_kg` nul ou `≥ 0` : contraintes `variante_prix_strictement_positif`, `variante_prix_promo_coherent`, `variante_poids_positif`.
  - `prix_effectif` = promo si elle existe, sinon prix.
- `Stock` : un par variante (créé automatiquement). `quantite_disponible ≥ 0`, `seuil_alerte`. `decrementer()` / `incrementer()` : UPDATE SQL atomiques.
- `ImageProduit` : galerie, **10 images au maximum** par produit (`MAX_IMAGES_PAR_PRODUIT`).

### Montants en FCFA

Les colonnes restent en `DecimalField(12, 2)`, mais **l'API refuse tout montant non entier** (`prix_base`, `prix`, `prix_promo`) : 400 « Les montants sont en FCFA et doivent être entiers (les paiements mobile money ne gèrent pas de centimes). » `poids_kg` accepte des décimales.

### Visibilité publique

`Produit.objects.visibles_publiquement()` = `publies()` (produit actif, boutique publiable) **et au moins une variante active**. Sans variante active, rien ne peut être mis au panier : le produit est absent des listes et sa fiche répond 404. Les variantes inactives n'apparaissent jamais sur la fiche.

C'est **la seule** règle de visibilité publique : la vérification publique d'un passeport QR l'utilise telle quelle (produit non visible → certificat affiché, mais « Vendeur indisponible », `disponible_a_la_vente: false`, `produit_slug: null`). Toute évolution de `visibles_publiquement()` s'applique donc aux deux.

#### Vues SQL (migration 0004) : elles font partie de la règle

FastAPI (recherche, listes publiques) ne lit pas les tables du catalogue : il lit trois vues qui traduisent ces règles en SQL.

| Vue | Traduit | Colonnes |
|---|---|---|
| `catalogue_produit_public` | `Produit.objects.visibles_publiquement()`, prix affiché et `en_stock` de `ProduitPublicListView`, image principale de `ProduitPublicListSerializer` | `id`, `nom`, `slug`, `prix_base`, `date_creation`, `categorie_id`, `categorie_nom`, `categorie_slug`, `categorie_parent_id`, `categorie_parent_slug`, `boutique_id`, `boutique_nom`, `boutique_slug`, `prix_min`, `en_stock`, `image_principale` (chemin relatif au dossier média), `nom_normalise`, `texte_normalise`, `miniature_principale` (chemin relatif de la miniature de **la même image** que `image_principale` ; `NULL` tant qu'elle n'existe pas ; colonne ajoutée **en fin de liste** par la migration 0005) |
| `catalogue_categorie_publique` | `Categorie.objects.actives()` | `id`, `nom`, `slug`, `parent_id`, `nom_normalise` |
| `catalogue_boutique_publique` | `Boutique.objects.publiques()` | `id`, `nom`, `slug`, `nom_normalise` |

- **Colonnes publiques uniquement** : `en_stock` est un booléen ; jamais la quantité, `seuil_alerte`, `sku`, `desactive_par`, ni aucune donnée du propriétaire de la boutique (email, téléphone, KYC, rôle). Le rôle FastAPI n'a de droit que sur ces vues.
- `nom_normalise` / `texte_normalise` = `catalogue_normaliser(...)` (minuscules, sans accents), fonction créée par la même migration et utilisée par les index de recherche. À ne pas confondre avec la colonne `vendeurs_boutique.nom_normalise` (anti-usurpation), qui n'est pas exposée.
- Toute évolution des règles ci-dessus (`visibles_publiquement()`, `publies()`, `est_publiable` / `publiques()`, `actives()`, prix affiché, `en_stock`, image principale de la liste) doit être **répercutée dans les vues** par une nouvelle migration. Le test de parité (`CatalogueVuesPubliquesTests`, § 11) échoue sinon.

## 3. Endpoints — `/api/catalogue/`

### Public (`AllowAny`, limite dédiée `catalogue_public` : 1200/heure par IP)

| Méthode | URL | Description |
|---|---|---|
| GET | `categories/` | Catégories racines actives et leurs **sous-catégories actives** (non paginé) |
| GET | `categories/<slug>/` | Détail d'une catégorie active |
| GET | `produits/` | Liste paginée des produits visibles (chaque produit porte `image_principale` et `miniature_principale`, § 3 ter). Filtres : `recherche` (nom, description, nom de boutique), `categorie` (slug **ou** id ; une catégorie au slug numérique comme « 2024 » est trouvée), `boutique` (slug ou id), `prix_min`, `prix_max` (entiers). Tri : `tri=prix_asc`, `prix_desc`, `date_asc` (défaut : plus récents) |
| GET | `produits/<slug>/` | Fiche : variantes actives, images, boutique, catégorie |

- **Prix affiché, filtré et trié** = plus petit prix effectif (promo comprise) des **variantes actives** (`prix_min` dans la liste). Une variante inactive n'influence ni filtre ni tri.
- **Stock public** : `variantes[].stock` vaut uniquement `{"est_en_stock": bool}`. Ni `quantite_disponible` ni `seuil_alerte` (données internes du vendeur, exploitables par un concurrent). Même règle que le panier.
- Liste : `en_stock` = au moins une variante active en stock.
- **En-tête `Authorization` facultatif** (`JWTAuthentificationOptionnelle`, `apps/core/authentification.py`) : un jeton valide identifie le compte (limite comptée par compte) ; un jeton expiré, révoqué, malformé ou à signature fausse est **ignoré** et la requête est traitée comme celle d'un visiteur (jamais 401). La signature est toujours vérifiée : un jeton forgé n'authentifie personne. La réponse ne dépend pas de l'identité.

### Espace vendeur (`IsAuthenticated` + `EstVendeurValide` + `BoutiqueDuVendeurNonSuspendue` + propriétaire)

| Méthode | URL | Description |
|---|---|---|
| GET / POST | `vendeur/produits/` | Liste paginée / création (`nom`, `description`, `categorie`, `prix_base`, `est_actif`). Un produit créé inactif est attribué au vendeur (`desactive_par = vendeur`) |
| GET / PUT / PATCH | `vendeur/produits/<id>/` | Détail et modification. `est_actif` : voir § 6 |
| DELETE | `vendeur/produits/<id>/` | **Désactivation** (`est_actif = false`, `desactive_par = vendeur`), **204**. Jamais de suppression |
| GET / POST | `vendeur/produits/<id>/variantes/` | Variantes d'un produit ; création avec `quantite_initiale`, `seuil_alerte` |
| GET / PUT / PATCH | `vendeur/variantes/<id>/` | Détail et modification. **`produit` n'est pas modifiable** : un autre produit → 400 `errors.produit` ; renvoyer le même (PUT) est accepté |
| DELETE | `vendeur/variantes/<id>/` | **Désactivation** (`est_active = false`), **204**. Réactivation : PATCH `est_active: true` |
| PUT / PATCH | `vendeur/variantes/<id>/stock/` | `quantite_disponible` (≥ 0), `seuil_alerte`. Écriture partielle sous verrou (§ 5) |
| GET / POST | `vendeur/produits/<id>/images/` | Galerie ; ajout (multipart `image`, `est_principale`, `ordre`). La miniature est générée dans la même requête (§ 3 ter) |
| DELETE | `vendeur/images/<id>/` | Suppression réelle d'une image (aucun historique n'en dépend) |

Accès :
- **403** : client, vendeur non validé (lecture comprise), administration et `is_staff` (l'administration modère via § 3 bis).
- **403 sur toute écriture** (POST, PUT, PATCH, DELETE, y compris stock et images) quand la boutique du vendeur est **suspendue** ; la lecture reste possible. Même règle que `ma-boutique/` et les passeports.
- **404** sur tout objet (produit, variante, stock, image) d'une autre boutique.
- Le vendeur voit le stock complet de ses variantes et `desactive_par` de ses produits (lecture seule).

### Refus (400)
- `prix` ≤ 0, `prix_promo` ≤ 0 ou ≥ `prix` (aussi quand un PATCH baisse `prix` sous la promo existante : `errors.prix_promo`), `prix_base` < 0, `poids_kg` < 0, montant non entier.
- Variante déplacée vers un autre produit : `errors.produit`.
- 11ᵉ image d'un produit : `errors.image` « Un produit ne peut pas avoir plus de 10 images… ». Image de plus de **5 Mo**, format autre que JPEG/PNG/WebP, ou contenu qui ne correspond pas à l'extension (signature binaire vérifiée) : `errors.image`.
- Stock négatif.

### 3 bis. Administration — `administration/produits/` et `administration/produits/<id>/` (`IsAuthenticated` + `EstAdministrateur`)

| Méthode | URL | Description |
|---|---|---|
| GET | `administration/produits/` | **Liste** paginée (20) des produits de toutes les boutiques, **inactifs compris**, plus récents d'abord. Filtres combinables : `recherche` (nom du produit ou de la boutique, 100 caractères au plus), `est_actif` (`true` ou `false`), `boutique` et `categorie` (identifiants) ; une valeur invalide est ignorée. Nombre de requêtes constant. Autres rôles et `is_staff` seul : **403** ; anonyme **401** |
| GET | `administration/produits/<id>/` | Produit de n'importe quelle boutique : `id`, `boutique`, `boutique_nom`, `categorie`, `nom`, `slug`, `est_actif`, `desactive_par`, dates (même représentation que la liste) |
| PATCH / PUT | `administration/produits/<id>/` | **Seul `est_actif` est modifiable.** Tout autre champ → 400 « Seul le champ est_actif est modifiable ici (champs refusés : …) », comme `est_suspendue` sur la boutique. Chaque changement est journalisé (logger `securite`) |

### 3 ter. Miniatures des images

Une liste de produits renvoie l'image originale (jusqu'à 5 Mo) : trop lourd en 3G pour 20 vignettes. Chaque `ImageProduit` a donc une `miniature`.

- **Format** : WebP, **480 px de large au plus** (jamais agrandie : une image plus petite garde sa taille), qualité 75, orientation EXIF appliquée (une photo prise en portrait n'est pas couchée), transparence conservée. Fichier dans `catalogue/miniatures/<année>/<mois>/`, nommé comme l'original (`.webp`). Logique dans `apps/catalogue/images.py` (`contenu_miniature`, `generer_miniature`).
- **Génération synchrone**, dans la requête d'envoi (`ImageProduitListCreateView`), **après** le bloc verrouillé qui limite à 10 images : un envoi lent ne retarde pas les autres. Coût (quelques centaines de ms pour 5 Mo) : hypothèse, à mesurer. L'envoi est rare (geste du vendeur).
- **Échec de génération** (image corrompue après validation, fichier illisible) : l'image est **enregistrée quand même**, `miniature` reste vide et une ligne est journalisée (`apps.catalogue.images`, niveau `WARNING`). Aucune erreur 500 : le frontend affiche l'original en repli.
- **API** : `ImageProduit.miniature` (URL absolue ou `null`, lecture seule) dans la galerie et la fiche produit ; `miniature_principale` (URL absolue ou `null`) dans la liste publique, **pour la même image que `image_principale`** (la principale, sinon la première) : jamais la miniature d'une autre image du produit. `image` et `image_principale` ne changent pas.
- **Images existantes** : `python manage.py generer_miniatures [--lot N]` (défaut 100). **Idempotente** (seules les images sans miniature sont traitées), par lots d'identifiants croissants (mémoire bornée), une image qui échoue n'est pas retentée dans la même exécution. Affiche `N miniature(s) générée(s), M échec(s).` Relançable à volonté ; à lancer une fois après `migrate`.
- **Vue SQL** : `miniature_principale` est exposée par `catalogue_produit_public` (§ 2) pour la recherche FastAPI, qui la renvoie sous le même nom ([`MODULE_RECHERCHE.md`](./MODULE_RECHERCHE.md) § 3). La migration doit être appliquée avant le déploiement de FastAPI.
- **Fichiers orphelins** : supprimer une image ne supprime pas ses fichiers (aucun `post_delete` dans le projet) ; il y a désormais deux fichiers orphelins par image au lieu d'un. À traiter avec la purge des médias (§ 9).
- **Hors périmètre** : logos et bannières de boutique (une image par boutique), à traiter de la même façon si la mesure le justifie.

## 4. Suppression : jamais par l'API

Un produit ou une variante peut figurer dans des commandes, des paniers, des passeports et des tickets. Supprimer cassait cet historique :
- commande → erreur 500 (`ProtectedError`) ;
- produit → passeports **et historique de scans effacés** ;
- variante → certificat modifié (variante retirée), voire 500 en cas de collision de lot ;
- panier → lignes effacées sans prévenir le client.

Règle : **DELETE = désactivation** (204). En filet de sécurité pour le Django admin, `CommandeItem.variante`, `PasseportProduit.produit/variante` et `SupportTicket.product` sont en **PROTECT** : une suppression réelle d'un élément référencé est refusée.

## 5. Stock et concurrence

- Checkout (`commandes.ValiderPanierView`) : verrou `select_for_update` sur les lignes de stock, puis `decrementer()` (UPDATE conditionnel, jamais de stock négatif).
- Mise à jour par le vendeur (`StockUpdateSerializer.update`) : relit la ligne **sous le même verrou** et **n'écrit que les champs envoyés** : modifier seulement `seuil_alerte` ne réécrit pas la quantité lue en début de requête (sinon des ventes faites entre-temps « réapparaîtraient » en stock).
- `quantite_disponible` envoyée par le vendeur reste une valeur **absolue** (voir dette § 9).

## 6. Désactivation, réactivation et modération

| Qui a désactivé le produit | Le vendeur peut réactiver ? | L'administration peut réactiver ? |
|---|---|---|
| Le vendeur (DELETE, PATCH `est_actif: false`, création inactive) | Oui (PATCH `est_actif: true` → 200) | Oui |
| L'administration (`administration/produits/<id>/`, ou Django admin) | **Non : 403** « Ce produit a été désactivé par l'administration ANITCHE : seule l'administration peut le réactiver. » | Oui |

- Une désactivation de l'administration **s'impose** à celle du vendeur. Un vendeur qui redésactive (DELETE, PATCH) un produit désactivé par l'administration ne change rien (204/200 sans effet).
- Réactivation refusée = **tout ou rien** : aucun autre champ de la même requête n'est appliqué.
- Une modification d'autres champs n'écrit que ces champs : une sauvegarde du vendeur ne peut pas annuler une désactivation faite en même temps par l'administration.
- Le vendeur peut toujours modifier le contenu d'un produit désactivé ; il reste désactivé.
- Même mécanisme que `PasseportProduit` (voir `MODULE_PASSEPORT_QR.md`, § 5 bis).

## 7. Performance

- Liste publique : `select_related` boutique et catégorie, images préchargées, prix minimum (`Min` + `Coalesce`) et disponibilité (`Exists`) calculés en SQL. **Nombre de requêtes constant**, quel que soit le nombre de produits.
- Liste vendeur : `select_related('categorie', 'boutique__proprietaire')`, images et `variantes__stock` préchargées (`est_achetable` sans requête supplémentaire par produit).
- Fiche : variantes actives préchargées (`Prefetch(..., to_attr='variantes_actives')`), sous-catégories actives préchargées.
- Pagination : 20 par page (réglage global).

## 8. Limite de débit

`catalogue_public` : **1200/heure par IP** pour un visiteur (par compte s'il est connecté), sur toutes les routes publiques du catalogue. Avec le taux `anon` (50/heure) partagé avec toute l'API, quelques minutes de navigation suffiraient à l'épuiser, surtout derrière le CGNAT des opérateurs mobiles, où beaucoup de clients partagent une IP publique. L'identifiant du visiteur passe par `REST_FRAMEWORK['NUM_PROXIES']` : changer `X-Forwarded-For` ne contourne pas la limite (testé).

Le seau est **partagé** avec les boutiques publiques (`/api/vendeurs/boutiques/`, liste et fiche) et la grille des tarifs de livraison (`/api/livraison/tarifs/`) : c'est une même navigation. Aucune de ces routes ne consomme `anon` ni `user` (`throttle_classes = [ScopedRateThrottle]`, testé). Deux comptes connectés derrière la même IP ont chacun leur seau ; un jeton refusé compte par IP, comme un visiteur (testé).

## 9. Dette connue

- **Niveau 2 — ajustement relatif du stock** : ajouter un endpoint de réassort (`+N` / `−N`, UPDATE `F()`), pour qu'un vendeur n'écrase jamais une vente avec une valeur absolue lue avant elle. Le verrou actuel ordonne les écritures mais ne change pas la sémantique « valeur absolue ».
- **`seuil_alerte` n'est utilisé nulle part** : aucune notification de stock faible. À brancher sur le module `notifications`.
- **Recherche de la liste Django sans index** : les index de recherche existent depuis la migration 0004 (trigrammes sur `catalogue_normaliser(nom)` et `catalogue_normaliser(nom || ' ' || description)`, `(date_creation DESC, id DESC)`), mais seule la recherche FastAPI s'en sert. La liste Django garde `icontains` sur nom, description et nom de boutique : sensible aux accents, phrase entière exigée, parcours complet de la table. Son tri par date n'utilise pas non plus l'index (les annotations `Min` / `Exists` calculent tous les produits avant le tri). À reprendre seulement si la liste Django sert au-delà du repli de FastAPI.
- **Niveau 2, seulement sur mesure** : tri par prix et facettes du catalogue entier calculent le prix minimum de chaque produit (≈ 0,9 s à 100 000 produits synthétiques dans la vue, ≈ 0,6 s dans la liste Django ; sous 70 ms à 10 000). Colonnes `prix_min` / `en_stock` dénormalisées et tenues à jour par Django si un test de charge le justifie, pas avant.
- **Produit rangé dans une catégorie inactive** : il reste visible et filtrable par l'id de sa catégorie (vue `catalogue_produit_public` comprise). À décider (masquer, ou interdire le rattachement à une catégorie inactive) ; la vue et le test de parité suivront.
- Les images de catégorie et la gestion des catégories ne passent que par le Django admin (aucune API).
- **Fichiers d'images orphelins** : ni l'original ni la miniature ne sont supprimés du stockage quand une `ImageProduit` est supprimée (aucun `post_delete`). À traiter avec une purge des médias (niveau 2).

## 10. Points du contrat à connaître côté frontend

| Point | Comportement |
|---|---|
| Vendeur suspendu | 403 sur toute écriture, lecture possible |
| `produit` d'une variante | Non modifiable : 400 `errors.produit` |
| Fiche publique : `stock` | `{"est_en_stock": bool}` seulement (ni quantité ni seuil) |
| DELETE produit/variante | Désactivation, 204 (commandes, passeports et paniers conservés) |
| Produit sans variante active | Absent des listes, fiche 404 |
| Prix nuls, négatifs, promo incohérente, poids négatif, centimes | 400 (et contraintes en base) |
| Modération | `administration/produits/<id>/` (`est_actif` seulement) ; champ `desactive_par` (lecture seule côté vendeur) ; vendeur : 403 sur la levée d'une désactivation de l'administration |
| Images | 5 Mo (règle du modèle), 10 par produit |
| Sous-catégories inactives | Masquées |
| Filtres et tri de prix | Prix effectif minimum des variantes actives |
| `categorie=<nombre>` | id **ou** slug |
| Limite publique | `catalogue_public` 1200/h (pas le taux `anon` partagé), seau commun avec les boutiques publiques et les tarifs |
| Jeton expiré ou révoqué | Ignoré sur les routes publiques : 200 comme un visiteur, jamais 401 |

Collections Postman : `postman_paiements.json` et `postman_0_setup_vendeur.json` lisent `v.stock.est_en_stock` sur la fiche publique.

## 11. Tests

⚠️ **Toujours lancer les tests sur PostgreSQL.**

```bash
cd backend-django
DJANGO_SETTINGS_MODULE=config.settings.ci DB_NAME=anitche_test DB_USER=postgres DB_PASSWORD=postgres DB_HOST=localhost \
  python manage.py test apps.catalogue -v 2
```

Vérifier dans la sortie `-v 2` que tout est `ok` et rien `skipped`. Les tests d'images écrivent dans un `MEDIA_ROOT` temporaire (supprimé en fin de classe), jamais dans `media/`.

Couverture (`apps/catalogue/tests.py`, classes `Catalogue*Tests`) : suspension (9 écritures), isolation (12 routes, `is_staff`, administration), variante non déplaçable, stock public, visibilité (produit sans variante active, variante inactive, sous-catégories, prix effectif, slug numérique), désactivation au lieu de suppression (commandes, paniers, passeports, tickets, PROTECT en base), modération (12 cas), prix (règles API et contraintes en base), stock (mise à jour concurrente, négatif), images (10 max, contenu falsifié, 5 Mo), N+1 (nombre de requêtes constant), limite de débit (par IP, par compte avec un jeton valide), jeton expiré ignoré sur les quatre routes publiques.

Miniatures (`CatalogueMiniaturesTests`, `MigrationMiniaturesTests`) : grande image réduite en WebP de 480 px (rapport conservé), petite image jamais agrandie, orientation EXIF appliquée, échec de génération sans erreur 500 (image enregistrée, `miniature` nulle, ligne de journal), liste publique et fiche (`miniature_principale`, `miniature`, originaux inchangés), commande `generer_miniatures` (lots, rapport, idempotence), migration 0005 dans les deux sens ; la parité de la vue SQL (`CatalogueVuesPubliquesTests`) compare `miniature_principale` à la liste Django et vérifie qu'elle vient de la même image que `image_principale`. Liste d'administration (`CatalogueAdministrationListeTests`) : inactifs compris, chaque filtre, représentation, refus par rôle, requêtes constantes.

Vues SQL (`CatalogueVuesPubliquesTests`) : pour chaque cas de visibilité (produit désactivé par le vendeur ou par l'administration, boutique suspendue ou fermée, vendeur KYC en attente ou refusé, redevenu client ou inactif, variantes toutes inactives, sans variante, rupture de stock, promotion, catégorie inactive ou absente, image principale), les produits de `catalogue_produit_public` = `visibles_publiquement()` et la liste publique Django, avec les mêmes prix affichés, `en_stock`, image et champs de liste ; boutiques et catégories publiques identiques ; colonnes exposées = liste autorisée, aucune colonne sensible. Sous SQLite (`config.settings.test`), les vues ne sont pas créées et ces tests sont ignorés : d'où la règle « toujours PostgreSQL ».

**Postman** : `postman_catalogue.json` (hors dépôt) — 1. Connexions et lecture, 2. Administration (remise en état), 3. Préparation vendeur, 4. Parcours public, 5. Parcours vendeur, 6. Scénarios sécurité, 7–8. Nettoyage. Rejouable : produits et variantes retrouvés par leur nom et créés seulement s'ils manquent ; chaque scénario remet l'état qu'il modifie. `admin_password` à renseigner à la main. Les images ne sont pas couvertes (fichier à joindre manuellement).

## 12. Migrations

- **catalogue 0003** `desactivation_et_regles_de_prix` :
  1. vérifie les données : prix de base négatifs, prix ≤ 0, promo incohérente, poids négatif. S'il y en a, **s'arrête et liste les ids** ; aucune correction automatique (fixer un prix est une décision du vendeur) ;
  2. ajoute `desactive_par` ; les produits déjà inactifs sont attribués au **vendeur** (avant cette migration, seul le vendeur pouvait désactiver un produit par l'API) ;
  3. ajoute les contraintes du § 2.
- **passeport_qr 0006** et **support 0005** : `on_delete=PROTECT` (aucune opération SQL, la règle est appliquée par Django).

Base de dev vérifiée avant application : 2 produits, 0 inactif, 0 donnée hors règles. Migrations appliquées en dev.

- **catalogue 0004** `recherche_publique` (PostgreSQL uniquement ; rien sous SQLite) :
  1. extensions `unaccent` et `pg_trgm` (`IF NOT EXISTS`, schéma `public`). « Trusted » : le propriétaire de la base suffit, pas besoin d'être superutilisateur. **Jamais supprimées** au retour arrière (elles peuvent servir ailleurs) ;
  2. fonction `catalogue_normaliser(text)` (`IMMUTABLE`, pour pouvoir être indexée) ;
  3. les trois vues du § 2 ;
  4. index `catalogue_produit_nom_trgm`, `catalogue_produit_texte_trgm` (GIN trigrammes, mêmes expressions que les colonnes de la vue) et `catalogue_produit_date_id` (`date_creation DESC, id DESC`). `CREATE INDEX` simple ; `CONCURRENTLY` (migration non atomique) le jour où la table sera grosse.

  Retour arrière (`migrate catalogue 0003`) : `DROP INDEX`, `DROP VIEW`, `DROP FUNCTION`. Les droits de FastAPI sur les vues disparaissent avec elles : relancer le script de droits après un nouveau `migrate`. Le SQL est dans le fichier de migration (listes `CREATION` / `SUPPRESSION`) ; `sqlmigrate` ne l'affiche pas (opération `RunPython`, nécessaire pour ne rien exécuter sous SQLite).

  Vérifié sur la base de dev : application, retour arrière, nouvelle application sans erreur ; 18 produits dans la vue, identiques à la liste Django (le produit sans variante active est absent), 0 écart de prix ou de stock ; les deux index trigrammes sont utilisés à travers la vue (plan générique, paramètres liés).

- **catalogue 0005** `miniatures_images_produit` :
  1. `ImageProduit.miniature` : `ImageField` nullable (`ALTER TABLE … ADD COLUMN`, sans réécriture de table ni verrou long sous PostgreSQL 16) ;
  2. PostgreSQL uniquement : `CREATE OR REPLACE VIEW catalogue_produit_public`, avec `miniature_principale` **en fin de liste** (seul ajout accepté par `CREATE OR REPLACE`). Le SQL est recopié dans la migration, comme pour 0004. Les droits accordés au rôle FastAPI en lecture seule sont **conservés** (vérifié sur une base jetable : retour à 0004, script de droits, puis 0005, et le rôle lit la vue, nouvelle colonne comprise) ;
  3. images existantes : `python manage.py generer_miniatures` (§ 3 ter), à lancer une fois après `migrate`.

  Retour arrière (`migrate catalogue 0004`) : la vue est supprimée puis recréée telle que 0004 l'a créée (une colonne ne se retire pas par `CREATE OR REPLACE`), puis la colonne est retirée. **La suppression de la vue retire les droits de FastAPI sur elle : relancer le script de droits.** Testé dans les deux sens (`MigrationMiniaturesTests`). Le test d'intégration FastAPI `test_product_view_exposes_only_public_columns` compare la liste **exacte** des colonnes de la vue : toute colonne ajoutée à la vue doit l'être aussi dans `backend-fastapi/tests/integration/test_recherche_vues.py`, dans le même commit.

### Règle : une migration qui modifie une colonne utilisée par ces vues doit supprimer puis recréer les vues

PostgreSQL refuse sinon (« cannot alter type of a column used by a view or rule » ; même refus pour supprimer la colonne). Changer le `max_length` d'un `CharField` est un changement de type. Colonnes utilisées, **modules vendeurs et utilisateurs compris** :

| Table | Colonnes |
|---|---|
| `catalogue_produit` | `id`, `nom`, `slug`, `description`, `prix_base`, `est_actif`, `date_creation`, `boutique_id`, `categorie_id` |
| `catalogue_varianteproduit` | `id`, `produit_id`, `prix`, `prix_promo`, `est_active` |
| `catalogue_stock` | `variante_id`, `quantite_disponible` |
| `catalogue_imageproduit` | `id`, `produit_id`, `image`, `miniature`, `est_principale`, `ordre` |
| `catalogue_categorie` | `id`, `nom`, `slug`, `parent_id`, `est_active` |
| `vendeurs_boutique` | `id`, `nom`, `slug`, `est_active`, `est_suspendue`, `proprietaire_id` |
| `utilisateurs_utilisateur` | `id`, `role`, `statut_kyc`, `is_active` |

Méthode : dans la même migration, `DROP VIEW` des vues concernées, l'opération Django, puis recréation des vues avec leur SQL recopié et mis à jour dans cette migration (une migration ne doit pas importer le code d'une autre, qui peut évoluer). Modifier `catalogue_normaliser` impose aussi de supprimer puis recréer les deux index trigrammes. Ensuite : relancer le test de parité sur PostgreSQL et le script de droits FastAPI.
