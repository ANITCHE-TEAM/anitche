# Module avis — contrat et règles

> Périmètre : backend Django, `backend-django/apps/avis/`, et les agrégats portés par `catalogue.Produit` et `vendeurs.Boutique`.
> Principe : **l'éligibilité repose sur l'achat, jamais sur le rôle** ; la note d'un produit et la réputation d'une boutique ne comptent que des avis publiés, recalculés sous verrou.
> État : publication, modification, suppression, listes de l'auteur, lecture publique par produit, réponse du vendeur, signalement et modération (masquage, rétablissement, rejet, tous tracés).

## 1. Ce sur quoi le module s'appuie

| Module | Lien |
|---|---|
| `commandes` | `CommandeItem` est la preuve d'achat ; la commande doit être `livree` et appartenir au client |
| `livraison` | `Livraison.date_livraison` (remise du colis avec le code du client) fait courir le délai pour noter |
| `catalogue` | `Produit.note_moyenne` et `Produit.nombre_avis` : agrégats écrits par le service des avis, jamais saisis |
| `vendeurs` | `Boutique.nombre_avis` et `Boutique.nombre_avis_positifs` : base du pourcentage d'avis positifs ; le propriétaire ne note pas sa boutique |
| `utilisateurs` | Permission `EmailVerifie` pour publier ; rôle `moderateur` pour la modération |
| `catalogue` (lecture) | `Produit.objects.visibles_publiquement()` décide si les avis d'un produit sont lisibles par le public |

## 2. Architecture

```text
views.py        HTTP : payloads, codes de réponse, limites de débit
services.py     éligibilité, verrous, recalcul des agrégats, écritures, réponse, signalement, modération
permissions.py  EstModerateurAvis (modérateur, admin, super_admin)
serializers.py  validation (note 1 à 5, commentaire 2000 caractères), représentations
admin.py        Django admin en lecture seule (avis, réponses, signalements, décisions)
```

## 3. Modèle `Avis`

`client`, `produit`, `boutique` (dénormalisée) et `commande_item` sont en `PROTECT` : un avis est un historique de vente. `note` (1 à 5), `commentaire` (facultatif), `statut` (`publie`, `masque`, `supprime`), `est_modifie`, dates.

Contraintes en base :

| Contrainte | Règle |
|---|---|
| `avis_unique_par_client_et_produit` | un avis par (client, produit), suppression douce comprise |
| `commande_item` OneToOne | une ligne de commande ne sert de preuve qu'une fois |
| `avis_note_entre_1_et_5` | note comprise entre 1 et 5 |
| `produit_note_coherente` | sans avis, pas de note ; avec des avis, une note entre 1 et 5 |
| `boutique_avis_positifs_coherents` | avis positifs ≤ avis |
| `signalement_unique_par_avis_et_auteur` | un signalement par (avis, signalant), même après la décision |
| `signalement_traitement_coherent` | `ouvert` sans décision ni date de traitement ; sinon les deux renseignés |
| `decision_moderation_motif_obligatoire` | motif non vide |
| `ReponseVendeur.avis` OneToOne | une réponse par avis |

Trois modèles s'ajoutent, tous en `PROTECT` et écrits par `services.py` seulement :

- **`ReponseVendeur`** : `avis` (OneToOne), `auteur` (le propriétaire), `contenu` (2000 caractères), `date_creation`, `date_modification`.
- **`SignalementAvis`** : `avis`, `auteur` (le signalant), `motif` (liste fermée : `spam`, `injurieux`, `hors_sujet`, `faux_avis`, `donnees_personnelles`, `autre`), `commentaire` (500 caractères, facultatif), `statut` (`ouvert`, `accepte`, `rejete`), `decision`, `date_creation`, `date_traitement`.
- **`DecisionModeration`** : `avis`, `moderateur`, `type_decision` (`masquer`, `retablir`, `rejeter_signalement`), `motif` (1000 caractères, obligatoire), `date_creation`. Qui, quand, quoi, pourquoi ; jamais modifiée ni supprimée par l'API.

## 4. Règles

| Sujet | Règle |
|---|---|
| Qui | Le client de la commande, compte à email vérifié, quel que soit son rôle. Le propriétaire de la boutique ne note pas ses produits (403) |
| Quand | Commande `livree`, au plus `AVIS_DELAI_JOURS` (60) jours après `Livraison.date_livraison` |
| Combien | Un avis par produit : un second article du même produit, ou un produit dont l'avis a été supprimé, donne 409 |
| Contenu | Note seule autorisée ; commentaire de 2000 caractères au plus |
| Modification | Par l'auteur, pendant `AVIS_DELAI_MODIFICATION_JOURS` (14) jours ; l'avis est marqué `est_modifie` ; refusée sur un avis masqué (403) |
| Suppression | Suppression douce par l'auteur ; l'avis sort des agrégats ; refusée sur un avis masqué (403) |
| Retour remboursé | L'avis reste publié et compté |
| Annulation | Sans effet : une commande annulée n'a jamais été livrée |
| Produit désactivé | Reste notable : l'avis porte sur un achat réel |

## 5. Agrégats et concurrence

Toute écriture ouvre une transaction, verrouille la ligne `Produit` puis la ligne `Boutique` (toujours dans cet ordre), écrit, puis recalcule depuis les avis `publie` : `Avg` et `Count` du produit, `Count` et `Count(note >= 4)` de la boutique. Deux avis simultanés sont appliqués l'un après l'autre et la note finale est celle d'un calcul séquentiel. La note est arrondie au centième supérieur à partir de la moitié.

## 6. Routes (`/api/avis/`)

| Route | Rôle | Réponses |
|---|---|---|
| `POST /` | acheteur, email vérifié | 201, 400 (non livrée, délai, champs), 403 (email, propre boutique), 404 (ligne d'un autre client), 409 (produit déjà noté), 429 |
| `GET a-noter/` | tout compte connecté | articles livrés, dans le délai, non notés, hors propre boutique |
| `GET mes-avis/` | tout compte connecté | avis de l'auteur, supprimés exclus |
| `GET <id>/` | auteur | 200, 404 (avis d'un autre ou supprimé) |
| `PATCH <id>/` | auteur | 200, 400 (corps vide, délai), 403 (masqué), 404, 429 |
| `DELETE <id>/` | auteur | 204, 403 (masqué), 404, 429 |
| `GET produit/<produit_id>/` | public (jeton facultatif) | avis **publiés** du produit, récents d'abord, paginés (20), avec `reponse_vendeur` ; 404 si le produit n'est pas visible au public |
| `GET recus/` | vendeur validé | avis publiés reçus par sa boutique, avec sa réponse ; `?sans_reponse=true` ne garde que ceux sans réponse |
| `POST <id>/reponse/` | propriétaire de la boutique de l'avis | 201, 400, 403, 404 (avis introuvable, masqué ou supprimé), 409 (déjà une réponse), 429 |
| `PATCH <id>/reponse/` | idem | 200, 403, 404 (pas de réponse), 429 |
| `POST <id>/signaler/` | tout compte connecté | 201, 400, 403 (son propre avis), 404 (avis masqué, supprimé ou inconnu), 409 (déjà signalé), 429 |
| `GET moderation/signalements/` | modérateur, admin, super_admin | file des signalements, paginée ; `?statut=ouvert` (défaut), `accepte` ou `rejete` ; valeur invalide : 400 |
| `POST moderation/avis/<id>/masquer/` | idem | 201 → la décision tracée ; 400 (motif absent), 404, 409 (avis non publié) |
| `POST moderation/avis/<id>/retablir/` | idem | 201 ; 400, 404, 409 (avis non masqué) |
| `POST moderation/signalements/<id>/rejeter/` | idem | 201 ; 400, 404, 409 (déjà traité) |

Limites de débit : `avis_creation` 10/heure, `avis_ecriture` 30/heure (modification, suppression, réponse du vendeur), `avis_signalement` 20/heure, par utilisateur ; lecture publique : `catalogue_public` (1200/heure par IP).

## 7. Réponse du vendeur

- Seul le **propriétaire de la boutique** de l'avis répond (compte vendeur validé). Un autre vendeur, un client, le modérateur ou l'admin : **403** ; anonyme : 401.
- Une réponse par avis (OneToOne) : la seconde est refusée (**409**), il faut la modifier. La modification n'a pas de limite de durée ; `date_modification` suit.
- Un avis masqué ou supprimé est introuvable (**404**) : on ne répond pas à un avis que le public ne voit plus, et la réponse existante disparaît avec lui.
- Boutique suspendue : **403** (écritures gelées côté vendeur, comme ailleurs).
- La réponse est publique (liste par produit) et visible de l'auteur de l'avis (`reponse_vendeur` dans ses routes). Elle ne change aucune moyenne.

## 8. Signalement et modération

**Signaler.** Tout compte connecté, une fois par avis (409 ensuite, **même après un rétablissement**), jamais sur son propre avis (403). Seul un avis publié se signale (404 sinon). Un signalement ne masque rien : il entre dans la file `ouvert`.

**Décider.** Modérateur, admin et super_admin, avec un **motif obligatoire** (1 à 1000 caractères, espaces seuls refusés) :

| Décision | Condition | Effet |
|---|---|---|
| `masquer` | avis `publie` | avis `masque` : sort de la liste publique, de `Produit.note_moyenne` / `nombre_avis` et des compteurs de la boutique (recalculés sous verrou) ; tous ses signalements ouverts passent à `accepte` et pointent vers cette décision. L'auteur voit `statut = masque` et ne peut plus modifier ni supprimer son avis |
| `retablir` | avis `masque` | avis `publie` de nouveau, moyennes recalculées |
| `rejeter_signalement` | signalement `ouvert` | signalement `rejete` ; l'avis reste publié, les autres signalements restent ouverts |

Un avis peut aussi être masqué sans signalement préalable. Chaque décision écrit une ligne `DecisionModeration` (avis, modérateur, action, motif, date). La file des signalements ouverts n'affiche que ceux dont l'avis est encore publié (un avis supprimé par son auteur en sort) ; `?statut=accepte` retrouve les avis masqués à rétablir.

**Droits du modérateur.** Le rôle `moderateur` ne donne accès qu'aux routes `moderation/` de ce module. Il n'est pas dans `ROLES_ADMINISTRATION` : recherche de comptes, journal des connexions, validation KYC lui sont refusés (403). `is_staff` n'est pas un critère.

**Concurrence.** Masquer et rétablir verrouillent produit, boutique puis avis (même ordre que les écritures de l'auteur) ; le rejet verrouille le signalement. Deux décisions simultanées sur le même objet s'appliquent l'une après l'autre (la seconde reçoit 409).

## 9. Impact frontend

Additif, aucun contrat existant ne change :

1. `AvisSerializer` (mes avis, détail) gagne `reponse_vendeur` (`null` ou `{contenu, date_creation, date_modification}`). `statut` vaut déjà `masque` pour un avis masqué : afficher « masqué par la modération ».
2. Fiche produit : `GET /api/avis/produit/<id>/` (public, paginé) ; auteur affiché « Prénom N. », jamais d'email.
3. Espace vendeur : `GET /api/avis/recus/` puis `POST` / `PATCH /api/avis/<id>/reponse/`.
4. Bouton « Signaler » : `POST /api/avis/<id>/signaler/` avec `motif` (`spam`, `injurieux`, `hors_sujet`, `faux_avis`, `donnees_personnelles`, `autre`) ; 409 = déjà signalé, 403 = son propre avis.
5. Portail de modération : file `GET /api/avis/moderation/signalements/`, décisions `masquer`, `retablir`, `rejeter` (corps `{motif}`).

## 10. Tests

⚠️ Toujours sur PostgreSQL (`config.settings.ci`) : la concurrence utilise `select_for_update()`.

- `apps/avis/tests.py` : socle (éligibilité, contraintes, agrégats, modification, listes, rôles, limites, concurrence).
- `apps/avis/test_moderation.py` — 61 tests : réponse du vendeur (propriétaire, unicité, modification, huit rôles refusés, boutique suspendue, avis masqué ou supprimé, visibilité, avis reçus), lecture publique (champs sans donnée personnelle, avis publiés seulement, produit non visible, pagination, nombre de requêtes constant), signalement (rôles, unicité en base et par l'API, motifs, avis masqué, aucun effet sur les moyennes, limite de débit aux taux de production), droits de modération (chaque rôle sur chaque route, modérateur sans autre pouvoir), file (ordre, représentation, filtre, pagination), masquer et rétablir (public, moyennes recalculées, signalements acceptés, traçabilité, motif obligatoire, 409), rejeter, admin Django en lecture seule.

## 11. Dette connue

- Rien n'empêche aujourd'hui un vendeur d'acheter ses propres produits (ajout au panier et validation acceptés). L'interdiction de noter sa propre boutique est portée par ce module ; l'interdiction d'acheter relève du panier et des commandes.
- La raison d'un masquage n'est pas montrée à l'auteur de l'avis ni au vendeur (seul le statut l'est) : à arbitrer.
- Aucune notification n'est envoyée à l'auteur ou au vendeur (avis masqué, réponse reçue).
