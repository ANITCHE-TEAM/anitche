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

## 3. Endpoints — `/api/catalogue/`

### Public (`AllowAny`, limite dédiée `catalogue_public` : 1200/heure par IP)

| Méthode | URL | Description |
|---|---|---|
| GET | `categories/` | Catégories racines actives et leurs **sous-catégories actives** (non paginé) |
| GET | `categories/<slug>/` | Détail d'une catégorie active |
| GET | `produits/` | Liste paginée des produits visibles. Filtres : `recherche` (nom, description, nom de boutique), `categorie` (slug **ou** id ; une catégorie au slug numérique comme « 2024 » est trouvée), `boutique` (slug ou id), `prix_min`, `prix_max` (entiers). Tri : `tri=prix_asc`, `prix_desc`, `date_asc` (défaut : plus récents) |
| GET | `produits/<slug>/` | Fiche : variantes actives, images, boutique, catégorie |

- **Prix affiché, filtré et trié** = plus petit prix effectif (promo comprise) des **variantes actives** (`prix_min` dans la liste). Une variante inactive n'influence plus ni filtre ni tri.
- **Stock public** : `variantes[].stock` vaut uniquement `{"est_en_stock": bool}`. Ni `quantite_disponible` ni `seuil_alerte` (données internes du vendeur, exploitables par un concurrent). Même règle que le panier.
- Liste : `en_stock` = au moins une variante active en stock.

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
| GET / POST | `vendeur/produits/<id>/images/` | Galerie ; ajout (multipart `image`, `est_principale`, `ordre`) |
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

### 3 bis. Administration — `administration/produits/<id>/` (`IsAuthenticated` + `EstAdministrateur`)

| Méthode | Description |
|---|---|
| GET | Produit de n'importe quelle boutique : `id`, `boutique`, `boutique_nom`, `nom`, `slug`, `est_actif`, `desactive_par`, dates |
| PATCH / PUT | **Seul `est_actif` est modifiable.** Tout autre champ → 400 « Seul le champ est_actif est modifiable ici (champs refusés : …) », comme `est_suspendue` sur la boutique. Chaque changement est journalisé (logger `securite`) |

## 4. Suppression : jamais par l'API

Un produit ou une variante peut figurer dans des commandes, des paniers, des passeports et des tickets. Supprimer cassait cet historique :
- commande → erreur 500 (`ProtectedError`) ;
- produit → passeports **et historique de scans effacés** ;
- variante → certificat modifié (variante retirée), voire 500 en cas de collision de lot ;
- panier → lignes effacées sans prévenir le client.

Règle : **DELETE = désactivation** (204). En filet de sécurité pour le Django admin, `CommandeItem.variante`, `PasseportProduit.produit/variante` et `SupportTicket.product` sont en **PROTECT** : une suppression réelle d'un élément référencé est refusée.

## 5. Stock et concurrence

- Checkout (`commandes.ValiderPanierView`) : verrou `select_for_update` sur les lignes de stock, puis `decrementer()` (UPDATE conditionnel, jamais de stock négatif).
- Mise à jour par le vendeur (`StockUpdateSerializer.update`) : relit la ligne **sous le même verrou** et **n'écrit que les champs envoyés**. Auparavant, modifier seulement `seuil_alerte` réécrivait la quantité lue en début de requête : des ventes faites entre-temps « réapparaissaient » en stock.
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

- Liste publique : `select_related` boutique et catégorie, images préchargées, prix minimum (`Min` + `Coalesce`) et disponibilité (`Exists`) calculés en SQL. **Nombre de requêtes constant**, quel que soit le nombre de produits (avant : ≈ 4 requêtes par produit).
- Liste vendeur : `select_related('categorie', 'boutique__proprietaire')`, images et `variantes__stock` préchargées (avant : ≈ 2 requêtes par produit, dues à `est_achetable`).
- Fiche : variantes actives préchargées (`Prefetch(..., to_attr='variantes_actives')`), sous-catégories actives préchargées.
- Pagination : 20 par page (réglage global).

## 8. Limite de débit

`catalogue_public` : **1200/heure par IP** pour un visiteur (par compte s'il est connecté), sur toutes les routes publiques du catalogue. Auparavant, le catalogue partageait le taux `anon` (50/heure) avec toute l'API : quelques minutes de navigation suffisaient à l'épuiser, surtout derrière le CGNAT des opérateurs mobiles, où beaucoup de clients partagent une IP publique. L'identifiant du visiteur passe par `REST_FRAMEWORK['NUM_PROXIES']` : changer `X-Forwarded-For` ne contourne pas la limite (testé).

## 9. Dette connue

- **Niveau 2 — ajustement relatif du stock** : ajouter un endpoint de réassort (`+N` / `−N`, UPDATE `F()`), pour qu'un vendeur n'écrase jamais une vente avec une valeur absolue lue avant elle. Le verrou actuel ordonne les écritures mais ne change pas la sémantique « valeur absolue ».
- **`seuil_alerte` n'est utilisé nulle part** : aucune notification de stock faible. À brancher sur le module `notifications`.
- **Recherche sans index** : `icontains` sur nom, description et nom de boutique. Niveau 2 : recherche plein texte ou trigrammes PostgreSQL (`pg_trgm`), selon les mesures.
- **Produit rangé dans une catégorie inactive** : il reste visible et filtrable par l'id de sa catégorie. À décider (masquer, ou interdire le rattachement à une catégorie inactive).
- Les images de catégorie et la gestion des catégories ne passent que par le Django admin (aucune API).

## 10. Changements de contrat (refonte de septembre 2026)

| Avant | Après |
|---|---|
| Vendeur suspendu : toutes les écritures acceptées | 403 sur toute écriture, lecture possible |
| `produit` d'une variante modifiable (y compris vers le produit d'un autre vendeur) | 400 `errors.produit` |
| Fiche publique : `stock` complet (`id`, `quantite_disponible`, `seuil_alerte`, `est_en_stock`, `date_mise_a_jour`) | `stock` = `{"est_en_stock": bool}` |
| DELETE produit/variante = suppression (500 si commandé ; passeports et paniers effacés) | DELETE = désactivation, 204 |
| Produit sans variante active visible (`est_achetable: true`, `variantes: []`) | Absent des listes, fiche 404 |
| Prix nuls, négatifs, promo incohérente, poids négatif, centimes acceptés | 400 (et contraintes en base) |
| Pas de modération par l'API | `administration/produits/<id>/` (`est_actif` seulement) ; nouveau champ `desactive_par` (lecture seule côté vendeur) ; vendeur : 403 sur la levée d'une désactivation de l'administration |
| Images : 3 Mo (serializer), nombre illimité | 5 Mo (règle du modèle), 10 par produit |
| Sous-catégories inactives publiques | Masquées |
| Filtres et tri de prix sur `prix`/`prix_base`, variantes inactives comprises | Prix effectif minimum des variantes actives |
| `categorie=<nombre>` : id uniquement | id **ou** slug |
| Limite publique `anon` 50/h (partagée) | `catalogue_public` 1200/h |

Collections Postman adaptées : `postman_paiements.json` et `postman_0_setup_vendeur.json` lisent désormais `v.stock.est_en_stock` sur la fiche publique.

## 11. Tests

⚠️ **Toujours lancer les tests sur PostgreSQL.**

```bash
cd backend-django
DJANGO_SETTINGS_MODULE=config.settings.ci DB_NAME=anitche_test DB_USER=postgres DB_PASSWORD=postgres DB_HOST=localhost \
  python manage.py test apps.catalogue -v 2
```

Vérifier dans la sortie `-v 2` que tout est `ok` et rien `skipped`. Les tests d'images écrivent dans un `MEDIA_ROOT` temporaire (supprimé en fin de classe), jamais dans `media/`.

Couverture (`apps/catalogue/tests.py`, classes `Catalogue*Tests`) : suspension (9 écritures), isolation (12 routes, `is_staff`, administration), variante non déplaçable, stock public, visibilité (produit sans variante active, variante inactive, sous-catégories, prix effectif, slug numérique), désactivation au lieu de suppression (commandes, paniers, passeports, tickets, PROTECT en base), modération (12 cas), prix (règles API et contraintes en base), stock (mise à jour concurrente, négatif), images (10 max, contenu falsifié, 5 Mo), N+1 (nombre de requêtes constant), limite de débit.

**Postman** : `postman_catalogue.json` (hors dépôt) — 1. Connexions et lecture, 2. Administration (remise en état), 3. Préparation vendeur, 4. Parcours public, 5. Parcours vendeur, 6. Scénarios sécurité, 7–8. Nettoyage. Rejouable : produits et variantes retrouvés par leur nom et créés seulement s'ils manquent ; chaque scénario remet l'état qu'il modifie. `admin_password` à renseigner à la main. Les images ne sont pas couvertes (fichier à joindre manuellement).

## 12. Migrations

- **catalogue 0003** `desactivation_et_regles_de_prix` :
  1. vérifie les données : prix de base négatifs, prix ≤ 0, promo incohérente, poids négatif. S'il y en a, **s'arrête et liste les ids** ; aucune correction automatique (fixer un prix est une décision du vendeur) ;
  2. ajoute `desactive_par` ; les produits déjà inactifs sont attribués au **vendeur** (avant cette refonte, seul le vendeur pouvait désactiver un produit par l'API) ;
  3. ajoute les contraintes du § 2.
- **passeport_qr 0006** et **support 0005** : `on_delete=PROTECT` (aucune opération SQL, la règle est appliquée par Django).

Base de dev vérifiée avant application : 2 produits, 0 inactif, 0 donnée hors règles. Migrations appliquées en dev.
