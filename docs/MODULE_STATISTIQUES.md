# Module statistiques — contrat et règles

> Périmètre : backend Django, `backend-django/apps/statistiques/`. Lecture seule : aucun modèle, aucune migration.
> Principe : **une seule définition de « vente » pour tous les indicateurs**, des montants entiers XOF calculés depuis les montants figés dans les commandes, des agrégats SQL (jamais de boucle Python sur les commandes).
> État : statistiques du vendeur (sa boutique) et de l'administration (la plateforme), avec filtre de période.

## 1. Ce sur quoi le module s'appuie

| Module | Lien |
|---|---|
| `commandes` | `Commande` (statut, `created_at`) et `CommandeItem` (`prix_unitaire`, `quantite`, `montant_commission`, `montant_frais_fixes` figés au checkout) |
| `paiements` | `Reversement` : statut et montants (`montant_net`, `montant_a_verser`) ; un reversement `annule` retire sa commande des ventes |
| `retours` | `DemandeRetour` / `RetourItem` : articles retournés et remboursés ; retours acceptés pour le taux de retour |
| `avis` | `Avis` publiés (note moyenne, nombre) et `SignalementAvis` ouverts |
| `utilisateurs` | Nouveaux comptes `client` |
| `vendeurs` | `Boutique` du compte connecté ; permissions `EstVendeurValide` et `EstAdministrateur` |

## 2. Définition d'une vente

| Question | Règle |
|---|---|
| Quelles commandes **comptent** ? | Statuts `confirmee`, `preparation`, `expediee`, `livree` (payées, non annulées), **et** reversement non `annule` |
| Quelles commandes sont **exclues** ? | `creee` (non payée, expire à 30 minutes) ; `annulee` (remboursée si elle avait été payée) ; toute commande dont le reversement est `annule`, c'est-à-dire entièrement remboursée : une livraison `livree` dont la contestation « colis non reçu » est jugée fondée garde son statut mais ne compte plus. Une commande sans reversement (cas hérité) compte si son statut le permet |
| À quelle période appartient-elle ? | À celle de `Commande.created_at`, en jours entiers **Africa/Abidjan**, pour tous les indicateurs (reversements compris : par la date de leur commande) |
| Ventes brutes | Somme de `prix_unitaire × quantite` des articles : ni frais de livraison, ni remise de coupon (supportée par ANITCHE). Même base que `Reversement.montant_brut` |
| **Chiffre d'affaires** (vendeur) / **volume des ventes** (admin) | Ventes brutes **moins la valeur des articles retournés dont le retour est remboursé** (`rembourse`, `cloture`) des commandes comptées. Un retour seulement approuvé, en transit ou reçu ne se déduit pas encore ; un retour rejeté ou annulé jamais |
| Panier moyen | Ventes brutes ÷ commandes comptées, arrondi au franc (demi-franc au supérieur) ; 0 sans commande. Calculé **avant** retours : c'est la valeur du panier à l'achat |
| Top 5 des produits | Classés par ventes brutes (puis quantité, puis identifiant) ; chaque ligne : `produit`, `nom` (figé dans la commande), `quantité`, `ventes`. Avant retours, comme le panier moyen |
| Commissions ANITCHE | Commissions + frais fixes des articles des commandes comptées, **moins la commission rendue au vendeur** sur les retours remboursés (même arrondi par ligne que `reversements.part_vendeur_retournee` ; le frais fixe reste acquis à ANITCHE). Les frais de livraison payés par le client ne sont pas inclus |
| Taux de retour | Commandes `livree` comptées ayant au moins un retour **accepté** (`approuve`, `en_transit`, `receptionne`, `rembourse`, `cloture`) ÷ commandes `livree` comptées, en pourcentage à une décimale ; `0.0` sans livraison |
| Commandes par statut | **Toutes** les commandes créées dans la période, y compris celles qui ne comptent pas, avec les six statuts toujours présents (0 si aucune) |
| Reversements | Mêmes étapes que `reversements.resume_vendeur`. **Versés** : somme des `montant_a_verser` des reversements `verse`. **À venir** (vendeur) / **en cours** (admin) : somme des `montant_net` des reversements `en_attente_livraison`, `en_retractation`, `suspendu`, `disponible`, `en_cours`. Les ajustements pas encore imputés n'y figurent pas |
| Note moyenne et nombre d'avis | Avis `publie` de la boutique **créés dans la période** (masqués et supprimés exclus) ; `note_moyenne` à deux décimales, `null` sans avis |
| Avis signalés en attente (admin) | Signalements `ouvert` : **état actuel, sans filtre de période** (c'est une file à traiter) |
| Nouveaux clients (admin) | Comptes de rôle `client` créés dans la période |
| Vendeurs actifs (admin) | Boutiques ayant au moins une commande comptée dans la période |

## 3. Endpoints — `/api/statistiques/`

| Méthode | URL | Accès | Description |
|---|---|---|---|
| GET | `vendeur/` | `IsAuthenticated` + vendeur au KYC validé | Indicateurs de **sa** boutique. La boutique vient du compte connecté ; un paramètre `boutique` est ignoré. Vendeur validé sans boutique : **404** |
| GET | `administration/` | `admin`, `super_admin` | Indicateurs de la plateforme. Vendeur, client, livreur, `is_staff` seul : **403** |

Anonyme : **401** sur les deux. Autres méthodes : **405**. Les deux routes sont soumises à la limite de débit `user` par défaut.

**Période** (les deux routes) : `date_debut` et `date_fin`, `AAAA-MM-JJ`, **inclusives**, jours entiers en Africa/Abidjan. Sans paramètre : les 30 derniers jours, aujourd'hui compris. `date_fin` seule : 30 jours se terminant à cette date ; `date_debut` seule : jusqu'à aujourd'hui. **400** : date illisible ou inexistante, `date_fin` avant `date_debut`, période de plus de **366 jours**. La réponse rappelle la période appliquée (`periode`).

**Réponse vendeur** :

```json
{
  "periode": {"date_debut": "2026-10-01", "date_fin": "2026-10-31"},
  "chiffre_affaires": 16900,
  "montant_retours": 3500,
  "nombre_commandes": 7,
  "commandes_par_statut": {"creee": 1, "confirmee": 3, "preparation": 1, "expediee": 1, "livree": 3, "annulee": 1},
  "panier_moyen": 2914,
  "top_produits": [{"produit": 12, "nom": "Robe wax", "quantite": 4, "ventes": 14000}],
  "avis": {"note_moyenne": 4.33, "nombre": 3},
  "taux_retour": 50.0,
  "reversements": {"verses": 5030, "a_venir": 7944}
}
```

**Réponse administration** : `periode`, `volume_ventes`, `montant_retours`, `commissions`, `nombre_commandes`, `commandes_par_statut`, `nouveaux_clients`, `vendeurs_actifs`, `reversements` (`en_cours`, `verses`), `avis_signales_en_attente`.

Tous les montants sont des **entiers XOF**. `taux_retour` et `note_moyenne` sont des nombres décimaux.

## 4. Performance

Chaque indicateur est un agrégat SQL (`Sum`, `Count` conditionnel, `Avg`), sans boucle Python sur les commandes. Le nombre de requêtes est **constant** : **8** pour le vendeur (boutique, commandes, articles, retours remboursés, retours acceptés, top produits, avis, reversements) et **6** pour l'administration (commandes, articles, retours remboursés, reversements, nouveaux clients, signalements), que la période contienne 10 ou 1 000 commandes. Les tests le vérifient avec et sans 40 commandes de plus.

Niveau 2, si les mesures le demandent : un cache court (quelques minutes) par (boutique, période), ou un agrégat quotidien pré-calculé. Rien de cela n'est nécessaire aujourd'hui et la capacité n'est pas mesurée (CLAUDE.md § 16).

## 5. Impact frontend

Ajout sans rupture : `GET /api/statistiques/vendeur/` alimente le tableau de bord vendeur, `GET /api/statistiques/administration/` celui de l'administration. Passer `date_debut` et `date_fin` ; afficher la période renvoyée plutôt que de la recalculer. Les types sont régénérés dans `frontend/packages/api-client`.

## 6. Dette connue

- La date de rattachement est la création de la commande : une commande créée le 31 à 23 h 50 et payée le 1er compte dans le mois du 31.
- Une commande entièrement remboursée est retirée des ventes par son reversement `annule`. Un remboursement partiel d'une commande sans retour n'existe pas dans le modèle actuel.
- Les frais de livraison encaissés par ANITCHE ne sont pas dans `commissions`.
- Pas de comparaison avec la période précédente ni de série par jour : à ajouter si le portail les demande.

## 7. Tests

```bash
cd backend-django
DJANGO_SETTINGS_MODULE=config.settings.ci python manage.py test apps.statistiques -v 2
```

`apps/statistiques/tests.py` — 37 tests sur un jeu de données construit à la main (la valeur attendue de chaque indicateur est écrite en dur) :

- **Vendeur** (`IndicateursVendeurTests`) : chiffre d'affaires (annulée, non payée, reversement annulé, hors période, retours remboursés / approuvés / rejetés), commandes comptées, commandes par statut, panier moyen, top 5 (le sixième exclu), note et nombre d'avis (masqué, supprimé, ancien et autre boutique exclus), taux de retour, reversements versés et à venir (annulés et commande sans reversement), isolation entre boutiques, paramètre `boutique` ignoré, vendeur sans vente, montants entiers.
- **Administration** (`IndicateursAdministrationTests`) : volume des ventes, commissions (commission rendue déduite, frais fixes acquis), commandes par statut, nouveaux clients, vendeurs actifs, reversements, avis signalés sans filtre de période, période vide.
- **Période** (`PeriodeTests`) : défaut de 30 jours, première et dernière seconde, un seul jour, fuseau (un instant écrit à +02:00 qui tombe la veille à Abidjan), bornes seules, valeurs invalides (400), 366 jours acceptés et 367 refusés.
- **Droits** (`DroitsTests`) : anonyme, vendeur sur l'administration (403), chaque autre rôle et `is_staff` seul, vendeur non validé, vendeur sans boutique, lecture seule (405).
- **Requêtes** (`NombreDeRequetesTests`) : 8 et 6 requêtes, identiques avec 40 commandes de plus.

Chaque règle de la définition a été cassée une à une pour vérifier qu'un test passe alors au rouge (statuts comptés, reversement annulé, retours déduits, retours approuvés déduits, bornes de période, fuseau, filtre de boutique, commission rendue, avis masqués, reversements versés, top 5, signalements, vendeurs actifs, taux de retour).
