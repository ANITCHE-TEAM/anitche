# Module avis — contrat et règles

> Périmètre : backend Django, `backend-django/apps/avis/`, et les agrégats portés par `catalogue.Produit` et `vendeurs.Boutique`.
> Principe : **l'éligibilité repose sur l'achat, jamais sur le rôle** ; la note d'un produit et la réputation d'une boutique ne comptent que des avis publiés, recalculés sous verrou.
> État : socle (publication, modification, suppression, listes de l'auteur). La lecture publique, la réponse du vendeur, le signalement et la modération arrivent dans les étapes suivantes.

## 1. Ce sur quoi le module s'appuie

| Module | Lien |
|---|---|
| `commandes` | `CommandeItem` est la preuve d'achat ; la commande doit être `livree` et appartenir au client |
| `livraison` | `Livraison.date_livraison` (remise du colis avec le code du client) fait courir le délai pour noter |
| `catalogue` | `Produit.note_moyenne` et `Produit.nombre_avis` : agrégats écrits par le service des avis, jamais saisis |
| `vendeurs` | `Boutique.nombre_avis` et `Boutique.nombre_avis_positifs` : base du pourcentage d'avis positifs ; le propriétaire ne note pas sa boutique |
| `utilisateurs` | Permission `EmailVerifie` pour publier |

## 2. Architecture

```text
views.py        HTTP : payloads, codes de réponse, limites de débit
services.py     éligibilité, verrous, recalcul des agrégats, écritures
serializers.py  validation (note 1 à 5, commentaire 2000 caractères), représentation
admin.py        Django admin en lecture seule
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

Limites de débit : `avis_creation` 10/heure, `avis_ecriture` 30/heure, par utilisateur.

## 7. Dette connue

Rien n'empêche aujourd'hui un vendeur d'acheter ses propres produits (ajout au panier et validation acceptés). L'interdiction de noter sa propre boutique est portée par ce module ; l'interdiction d'acheter relève du panier et des commandes.
