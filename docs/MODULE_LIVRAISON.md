# Module livraison — contrat et règles

> Périmètre : backend Django, `backend-django/apps/livraison/` (et ses points de contact dans `commandes`, `paiements`, `notifications`, `utilisateurs`).
> État : refonte de septembre 2026. **« Livrée » déclenche de l'argent** (délai de rétractation de 7 jours puis reversement au vendeur) : toute la conception part de là.

## 1. Ce sur quoi le module s'appuie, et qui s'appuie sur lui

| Module | Lien |
|---|---|
| `paiements` | `valider_paiement` crée la fiche (adresse reprise du checkout) ; « livrée » → `commandes.services.synchroniser_depuis_livraison` → `reversements.ouvrir_retractation` ; contestation → `suspendre_reversement`, puis `reprendre_reversement` ou `annuler_reversement` + `Remboursement` (`livraison_non_recue`) ; `reversements._reprendre` ne reprend pas un reversement dont la livraison est contestée |
| `commandes` | « expédiée » et « livrée » se répercutent sur la commande (409 si elle n'est pas prête) ; `annuler_commande` appelle `livraison.services.annuler_livraison_de` dans sa transaction ; l'abandon d'une livraison annule la commande « expédiée » (motif `livraison_echouee`) |
| `notifications` | Écoute le signal `livraison_status_change` (le client est prévenu à chaque étape, sauf « annulée », déjà couverte par l'annulation de la commande) |
| `utilisateurs` | Rôle `livreur` (nommé et retiré par l'administration) ; `telephone` du livreur montré au client pendant la tournée |
| `vendeurs` | `EstAdministrateur`, `EstVendeurValide`, `ROLES_ADMINISTRATION` (jamais `is_staff` seul) |

## 2. Architecture

```text
views.py        HTTP : payloads, queryset par rôle, codes de réponse
permissions.py  EstLivreurOuAdministrateur (accès à l'endpoint de statut)
services.py     seul point d'entrée des changements : table des transitions sous
                select_for_update, code de livraison, assignation, annulation, abandon,
                contestation, rôle livreur
serializers.py  une représentation par rôle (client, livreur, vendeur, administration)
tasks.py        envoi du code de livraison par email (Celery, après commit)
admin.py        Django admin : livreur (livreurs actifs seulement) et date estimée
```

## 3. Modèles

- **`Livraison`** : `commande` (1-1), `livreur`, `status`, `adresse_livraison` (texte d'une ligne, copié du checkout ; le point GPS n'y est pas copié : il est lu dans `GroupeCommande`, § 5), `date_livraison_estimee`, `date_expedition`, `date_livraison`, `tentatives` (passages « en cours »), `code_hash` (vérification), `code_chiffre` (Fernet, pour le réafficher au client et l'envoyer par email), `code_essais`. Le code est **effacé** dès que la livraison est livrée ou annulée.
- **`LivraisonHistorique`** : chaque changement (statut, assignation) avec `effectue_par`, **`role_acteur`** figé au moment de l'action (`livreur`, `administration`, `client`, `systeme`) et `commentaire`.
- **`ContestationLivraison`** (1-1 avec la livraison) : `motif`, `statut` (`ouverte` → `rejetee` / `fondee`), `commentaire_resolution`, `resolue_par`, dates.

## 4. Statuts et transitions

| De | Vers | Qui | Effet |
|---|---|---|---|
| `en_attente` | `expediee` | livreur assigné | commande → `expediee` (409 si elle n'est pas en préparation) |
| `expediee` | `en_cours` | livreur assigné | tentative + 1, **nouveau code** généré, visible par le client et envoyé par email |
| `expediee` / `en_cours` | `echouee` | livreur assigné, **motif obligatoire** (`commentaire`) | aucun effet sur la commande |
| `en_cours` | `livree` | livreur assigné **+ code du client** | commande → `livree`, rétractation du reversement ouverte (7 jours) |
| `echouee` | `en_cours` | administration : nouvelle tentative | au plus `LIVRAISON_TENTATIVES_MAX` (3) passages « en cours », nouveau code |
| `echouee` | `annulee` | administration : abandon (`abandonner/`) | commande « expédiée » annulée (`livraison_echouee`), stock restitué, remboursement à traiter, reversement annulé |
| `en_attente` | `annulee` | système : commande annulée (client, administration, expiration…) | code effacé, coordonnées masquées au livreur |

Tout le reste est refusé, **administration comprise** : **400** pour une transition absente de la table (saut d'étape, retour arrière, sortie de `livree` ou `annulee`), **403** pour une étape qui ne revient pas à l'appelant (l'administration ne fait pas les étapes du livreur, le livreur ne décide pas d'une nouvelle tentative). « livrée » et « annulée » sont définitifs : une correction après livraison passe par la contestation ou les retours.

Le livreur doit, à chaque transition, être **assigné**, avoir le **rôle livreur**, être **actif** et **ne pas être le propriétaire de la boutique** de la commande (vérifié sous verrou : une donnée ancienne ou un changement de rôle par le Django admin n'ouvre aucun droit).

## 5. Endpoints — `/api/livraison/`

### Tous les comptes authentifiés (queryset par rôle ; 404 hors périmètre)

| Méthode | Chemin | Détail |
|---|---|---|
| GET | `` | Administration : toutes, filtres `?status=` et `?contestation=ouverte`. Livreur : les siennes. Tout autre compte : celles de ses commandes |
| GET | `<uuid>/` | Même règle d'accès |
| GET | `<uuid>/historique/` | Même règle d'accès ; **404** pour un tiers (avant : 200 avec une liste vide). `acteur` (rôle) remplace l'identifiant ; l'administration voit aussi `effectue_par` |
| PATCH | `<uuid>/statut/` | Livreur ou administration (`EstLivreurOuAdministrateur`, puis contrôle fin dans le service). Corps : `status`, `commentaire?` (obligatoire pour `echouee`), `code?` (obligatoire pour `livree`). **200** : la livraison (représentation de l'appelant). **400** / **403** : § 4. **409** : commande pas prête. **429** : limite `livraison_statut` |
| POST | `<uuid>/contester/` | Client de la commande uniquement (404 sinon). Corps : `motif`. **201** : la contestation. **400** : pas livrée, délai de 7 jours écoulé, déjà contestée. **429** : limite `livraison_contestation` |

### Vendeur (`EstVendeurValide` ; livraisons de sa boutique, lecture seule)

| Méthode | Chemin | Détail |
|---|---|---|
| GET | `vendeur/` | `id`, `commande`, `numero_commande`, `status`, `livreur` (`{prenom}`), `date_livraison_estimee`, `date_expedition`, `date_livraison`, `created_at`, `updated_at`. **Jamais le code**, ni l'adresse et le téléphone (déjà dans `/api/commandes/vendeur/`) |
| GET | `vendeur/<uuid>/` | Idem ; 404 pour la livraison d'une autre boutique |

### Administration (`EstAdministrateur` : rôle admin, jamais `is_staff` seul)

| Méthode | Chemin | Détail |
|---|---|---|
| POST | `<uuid>/assigner/` | Corps : `livreur_id`, `date_livraison_estimee?` (pas dans le passé). Assignation ou réassignation tant que la livraison n'est ni livrée ni annulée. **400** : pas livreur, inactif, propriétaire de la boutique, livraison terminée, livreur introuvable |
| POST | `<uuid>/abandonner/` | Corps : `commentaire` (obligatoire). Livraison échouée uniquement (**400** sinon) |
| POST | `<uuid>/contestation/resoudre/` | Corps : `decision` (`rejetee` / `fondee`), `commentaire?`. **400** si déjà traitée, **404** sans contestation |
| GET | `livreurs/` | Livreurs actifs : `id`, `prenom`, `nom`, `email`, `telephone`, `livraisons_en_cours` |
| POST | `livreurs/nommer/` | Corps : `utilisateur_id`. Client → livreur. **400** : vendeur, demande vendeur en cours ou validée, propriétaire d'une boutique, autre rôle, compte désactivé, déjà livreur |
| POST | `livreurs/<id>/retirer/` | Livreur → client, **effet immédiat**. Réponse : `utilisateur` et `livraisons_a_reassigner` (`id`, `numero_commande`, `status`) |

### Représentations

| Champ | Client | Livreur | Vendeur | Administration |
|---|---|---|---|---|
| `livreur` | `{prenom, telephone}` — téléphone **seulement pendant `en_cours`**, sinon `null` | `{prenom}` | `{prenom}` | `{id, prenom, nom}` |
| `adresse_livraison`, `adresse` (commune, quartier, point de repère, téléphone, **point GPS** `latitude` / `longitude`), `telephone_contact` | oui | oui, **masqués** (`""` / `null`) une fois `livree` ou `annulee` | non | oui |
| `code_livraison` | seulement pendant `en_cours` | non | non | non |
| `tentatives` | non | oui | non | oui |
| `code_essais_restants` / `code_essais` | non | pendant `en_cours` | non | `code_essais` |
| `contestation`, `date_limite_contestation` | oui (`statut`, dates) | non | non | `contestation` (avec le motif) |

`telephone_contact` et `adresse.telephone` sont le numéro choisi par le client au checkout, jamais celui de son profil.

**Point GPS du lieu de livraison** (`adresse.latitude`, `adresse.longitude` : nombres, ou `null` si le client ne l'a pas donné au checkout, [`MODULE_COMMANDES.md`](./MODULE_COMMANDES.md) § 3 bis). Point précis du domicile du client : il suit **exactement** la règle de l'adresse, sauf qu'il n'est jamais montré au vendeur. Où chaque règle est appliquée :

| Qui | Règle | Où |
|---|---|---|
| Client | le point de ses propres commandes, à tout statut | `livraisons_visibles` (`commande__client=utilisateur`) → `LivraisonClientSerializer` → `_CoordonneesClientMixin.get_adresse` (`adresse_du_groupe(…, avec_position=True)`) |
| Livreur | seulement les livraisons qui lui sont assignées, et plus rien une fois `livree` ou `annulee` | `livraisons_visibles` (`livreur=utilisateur`) → `LivraisonLivreurSerializer.to_representation` : `adresse` → `null` pour `STATUTS_TERMINES`, donc le point avec |
| Vendeur | **jamais** | `LivraisonVendeurSerializer` n'hérite pas de `_CoordonneesClientMixin` ; côté commandes, `CommandeVendeurSerializer` appelle `adresse_du_groupe` sans point |
| Administration | toujours | `LivraisonAdministrationSerializer` (`_CoordonneesClientMixin`) ; Django admin de `GroupeCommande` |

Sans point, le suivi n'affiche ni distance ni estimation d'arrivée (aucune valeur inventée, pas de repli sur un « centre de commune »).

## 6. Code de livraison

- Généré à **chaque** passage « en cours » (6 chiffres, `secrets`), il remplace le précédent et remet les essais à zéro.
- **Haché** (`make_password`) pour la vérification, **chiffré** (`EncryptedCharField`, mêmes clés que les numéros de reversement) pour être réaffiché dans l'app et envoyé par email : jamais en clair en base. Un haché seul ne peut pas être réaffiché au client, d'où les deux colonnes.
- **Email** au client, quelles que soient ses préférences de notification (sans ce code, le colis ne peut pas être remis) : tâche `envoyer_code_livraison_email`, lancée après le commit. Seul l'identifiant de la livraison passe par Redis ; la tâche relit le code et n'envoie rien si la livraison n'est plus en cours.
- **5 essais** par code. Au-delà, même le bon code est refusé : le livreur passe la livraison « échouée », et l'administration relance une tentative, avec un nouveau code. Blocage journalisé (`securite`).
- **Client injoignable** : « échouée » avec motif ; aucun passage « livrée » sans le code, même pour l'administration.

## 7. Contestation « non reçu »

1. Le client conteste une livraison **livrée**, au plus tard `date_livraison` + `REVERSEMENT_DELAI_RETRACTATION_JOURS` (7 jours), une seule fois : le reversement est **suspendu**, et chaque administrateur actif est alerté (journal `securite` + notification).
2. Tant que la contestation est ouverte, le reversement ne reprend pas, même si un retour de la même commande se clôt.
3. L'administration tranche : **rejetée**, le reversement reprend (rétractation ou disponible selon la date) ; **fondée**, le reversement est annulé et un `Remboursement` `livraison_non_recue` est créé (à traiter comme les autres, [`MODULE_PAIEMENTS.md`](./MODULE_PAIEMENTS.md) § 6). Le client est notifié de la décision.

La commande et la livraison restent « livrées » : la contestation fondée est tracée par `ContestationLivraison` et le remboursement.

## 8. Rôle livreur

- **Devenir livreur** : un compte client est nommé par l'administration (`livreurs/nommer/`). Un vendeur, ou un compte qui a une demande vendeur en cours, ne peut pas l'être : un compte ne porte qu'un rôle, et un livreur-vendeur pourrait déclencher son propre reversement.
- **Retrait** (`livreurs/<id>/retirer/`) : le compte redevient client **immédiatement**. Il ne voit plus ses livraisons et ne peut plus agir dessus, et celles qui ne sont pas terminées sont renvoyées pour réassignation.
- **Suspension** : `is_active=False` (Django admin) coupe tout accès, le jeton est refusé (401).
- **Django admin** : le champ `livreur` ne propose que les livreurs actifs, applique la même règle (jamais le propriétaire), et l'assignation passe par le service (historique, journal). Seul le rôle admin peut modifier une fiche ; le statut n'y est jamais éditable.

## 9. Frais de livraison

Mis en place en septembre 2026 (avant : aucun frais, `montant_total` = articles − remise). Code : `apps/livraison/frais.py` (tarif applicable, frais d'une commande), `apps/commandes/services.calculer_checkout` (seul calcul des montants du checkout), `apps/paiements/reversements.py` (livraison offerte), `apps/retours/services.frais_livraison_a_rembourser`.

### Règles

1. **Forfait par commune, payé par le client et figé au checkout ; la zone est déduite par le serveur, jamais déclarée par le client.** `TarifLivraison` : `zone` (`abidjan`, `hors_abidjan`), `commune` (vide = tarif par défaut de la zone), `montant` (FCFA entiers), `est_actif`. Les tarifs de commune de la zone `abidjan` forment la **liste des communes du district** (migration 0005, administrable) : Abobo, Adjamé, Attécoubé, Cocody, Koumassi, Marcory, Plateau, Port-Bouët, Treichville, Yopougon, Anyama, Bingerville, Songon. **Commune reconnue → zone `abidjan` ; toute autre → `hors_abidjan`**, figée dans `GroupeCommande.livraison_zone`. Tarif : celui de la commune s'il est actif, sinon le tarif par défaut de sa zone (une commune du district désactivée reste à Abidjan). Une ville hors Abidjan peut aussi avoir un tarif propre (ex. Grand-Bassam). La commune est comparée sans casse, accents, espaces ni ponctuation (« Port-Bouët » = « PORT BOUET » = « port-bouet ») ; **une commune n'a qu'un tarif, toutes zones confondues** (contrainte en base), pour qu'aucune zone ne soit ambiguë. Un champ `zone` envoyé au checkout est **ignoré**. Le poids n'est pas utilisé au lancement.
2. **Une commande = un colis = des frais.** Un checkout de plusieurs boutiques paie des frais pour chaque commande.
3. **Les frais reviennent à ANITCHE.** Ils ne sont jamais reversés au vendeur et ne portent aucune commission. Le coupon ne les réduit jamais (remise sur les articles seulement). Ils ne rapportent aucun point de fidélité.
4. **Livraison offerte**, une option de la boutique (`Boutique.livraison_offerte`, réglée par le vendeur dans `ma-boutique/`) : le client ne paie rien, et le tarif est déduit du reversement du vendeur, **plafonné au net** de la commande. Si le tarif dépasse le net, le reste devient un `AjustementVendeur` (nature `livraison_offerte`) **une fois la commande livrée** : une commande annulée avant la livraison ne coûte rien au vendeur.
5. **Retours** : pour un motif imputable au vendeur (article manquant, produit défectueux, non conforme), les frais payés sont rendus **en totalité, une fois par commande**, inclus dans `montant_remboursement` et facturés au vendeur (`AjustementVendeur`, nature `frais_livraison_retour`). Ils ne sont jamais rendus pour un changement d'avis, une mauvaise taille ou un autre motif. Une demande rejetée ou annulée ne compte pas : une demande suivante peut les inclure.

**Annulation avant livraison, abandon, contestation « non reçu » fondée** : le remboursement porte sur tout `montant_total`, frais compris (inchangé). Une contestation fondée efface aussi le reste de livraison offerte **non encore déduit** d'un versement.

| Commande | `frais_livraison` (payé par le client) | `livraison_offerte` | `frais_livraison_vendeur` (jamais montré au client) | Reversement |
|---|---|---|---|---|
| Livraison payée | tarif | `false` | 0 | inchangé (`montant_livraison` = 0) |
| Livraison offerte | 0 | `true` | tarif | `montant_livraison` = min(tarif, net des articles), déduit du net |

`Commande.montant_total` = articles − remise + `frais_livraison` : c'est le montant payé en ligne. `Commande.montant_hors_livraison` (propriété) sert de base aux points de fidélité et aux remboursements d'articles. Tout est figé dans la commande : modifier un tarif ou l'option de la boutique ne change jamais une commande passée.

**Tarifs initiaux (migrations 0004 et 0005), provisoires** : Abidjan 1 500 (défaut et chacune des 13 communes), hors Abidjan 3 000 FCFA, à ajuster par l'équipe via l'API. Sans tarif par défaut actif pour la zone, la validation et la simulation répondent **503** (erreur de configuration journalisée) ; c'est pourquoi le tarif par défaut d'une zone ne peut pas être désactivé.

### Endpoints

| Méthode | Chemin | Accès | Détail |
|---|---|---|---|
| GET | `/api/livraison/tarifs/` | public (limite `catalogue_public`) | Menu déroulant du checkout, une requête SQL : `communes` (`commune`, `zone`, `montant` réellement appliqué : les 13 communes du district, et les villes hors Abidjan qui ont un tarif actif) et `autres_villes` (`zone` `hors_abidjan`, `montant` par défaut ; `null` sans tarif). Ex. : `{"communes": [{"commune": "Cocody", "zone": "abidjan", "montant": 1500}, …], "autres_villes": {"zone": "hors_abidjan", "montant": 3000}}` |
| GET, POST | `/api/livraison/admin/tarifs/` | `EstAdministrateur` | Liste paginée ; création (`zone`, `commune`, `montant`, `est_actif?`). Un tarif de commune en zone `abidjan` **ajoute la commune au district** |
| GET, PATCH | `/api/livraison/admin/tarifs/<uuid>/` | `EstAdministrateur` | Modification du `montant` et de `est_actif`. **400** : zone ou commune modifiée (créer un autre tarif), tarif par défaut désactivé, commune qui a déjà un tarif (quelle que soit sa zone), second tarif par défaut d'une zone, montant négatif. Pas de suppression (**405**) : on désactive |
| POST | `/api/commandes/simuler-frais/` | client authentifié (limite `commande_simulation` 120/h) | Montants du checkout avant validation, voir [`MODULE_COMMANDES.md`](./MODULE_COMMANDES.md) § 3 |

Création et modification des tarifs sont tracées (`modifie_par`, journal `securite`). Le Django admin les montre en lecture seule, comme les barèmes de frais vendeur.

## 10. Impact frontend

1. **Code de livraison (client).** Afficher `code_livraison` dans le suivi quand `status` = `en_cours` (il arrive aussi par email), avec la consigne : le donner au livreur **à la remise du colis seulement**.
2. **Livreur : saisie du code.** « Livrée » envoie `{"status": "livree", "code": "123456"}`. **400** : code manquant ou faux (`detail` annonce les essais restants, `code_essais_restants` dans la fiche), puis code bloqué, il faut proposer « échouée ». « Échouée » exige un `commentaire` (motif), sinon **400**.
3. **`livreur` devient un objet** (avant : un identifiant). Client : `{prenom, telephone}` (téléphone non nul pendant `en_cours` seulement) ; vendeur et livreur : `{prenom}` ; administration : `{id, prenom, nom}`.
4. **Nouveaux champs** : `numero_commande`, `adresse` (structurée : commune, quartier, point de repère, téléphone), `date_livraison_estimee`, et pour le client `contestation` et `date_limite_contestation`. `adresse_livraison` et `telephone_contact` sont conservés.
5. **Nouveau statut `annulee`** : fiches des commandes annulées (y compris les anciennes, reprises par migration). Libellé et couleur à prévoir.
6. **Historique** : `effectue_par` est remplacé par `acteur` (`livreur`, `administration`, `systeme`, `client`) ; l'administration garde `effectue_par`. Un tiers reçoit **404** (avant : 200 avec une liste vide).
7. **Contestation (client)** : bouton « Je n'ai pas reçu mon colis » tant que `date_limite_contestation` n'est pas passée et que `contestation` est `null` → `POST <id>/contester/` (`motif`). Afficher ensuite l'état de `contestation.statut`.
8. **Livreur** : après « livrée » ou « annulée », l'adresse et le téléphone du client arrivent vides (`""` / `null`) : ne pas les mettre en cache côté app.
9. **Espace vendeur** : nouvel écran de suivi, `GET /api/livraison/vendeur/`.
10. **Back-office** : liste des livreurs, nomination et retrait (avec les livraisons à réassigner), assignation avec date estimée, nouvelle tentative (`PATCH statut/` → `en_cours` sur une livraison échouée), abandon, contestations ouvertes (`?contestation=ouverte`) et décision.
11. **Administration** : ne plus proposer de transitions libres (400 / 403, § 4).
12. **Erreurs à prévoir** : **429** au-delà de 120 changements de statut par heure et par compte, ou de 10 contestations par heure.
13. **Frais de livraison (§ 9).** Checkout : **ne plus demander la zone** (champ ignoré par le serveur). Menu déroulant des communes construit depuis `GET /api/livraison/tarifs/` (`communes`, avec le tarif de chacune), plus une entrée « Autre ville (hors Abidjan) » avec saisie libre de la ville au tarif `autres_villes`. Pour Abidjan, **toujours passer par le menu** : une saisie libre (« Riviera », « Le Plateau », « Abidjan ») n'est pas reconnue et part au tarif hors Abidjan. Afficher les montants et la `zone` déduite renvoyés par `POST /api/commandes/simuler-frais/` avant le paiement (frais par commande, « livraison offerte »). `adresse` contient `zone` (déduite). Back-office : écran des tarifs (`admin/tarifs/`), où l'ajout d'un tarif de commune en zone Abidjan étend le district.
14. **Format d'erreur unifié (septembre 2026).** Les refus (400, 403, 409) portaient avant un simple `{"detail": …}` : ils suivent désormais le format commun `{success: false, status_code, detail, errors}`, `errors` toujours un objet clé → liste de messages (`{}` si aucun champ n'est en cause) ; le message reste dans `detail`. Assignation à un livreur inexistant → **400** `errors.livreur_id`.
15. **Point GPS (septembre 2026).** `adresse` porte `latitude` et `longitude` (nombres, ou `null` sans point) pour le client, le livreur et l'administration ; jamais dans la vue vendeur. App livreur : ouvrir la navigation vers ce point quand il existe, sinon se guider à l'adresse texte ; comme le reste de l'adresse, il arrive `null` après « livrée » ou « annulée » (ne pas le garder en cache).

## 11. Sécurité — failles corrigées (diagnostic de septembre 2026)

Chaque constat a été confirmé par un test jetable sur PostgreSQL avant correction, et chacun a son test permanent dans `apps/livraison/tests.py`.

| # | Faille | Correction | Test |
|---|---|---|---|
| D1 | Le livreur seul déclarait « livrée » et déclenchait le reversement | Code de livraison du client obligatoire (haché, 5 essais) ; contestation pendant 7 jours | `CodeDeLivraisonTests`, `ContestationTests` |
| D2 | N'importe quel utilisateur assignable (Django admin), dont le vendeur de la commande | Assignation par l'API ou par le Django admin restreint : livreur actif, jamais le propriétaire ; revérifié à chaque transition | `AssignationTests` |
| D3 | Un ex-livreur gardait la main sur ses livraisons | Rôle livreur vérifié à chaque transition ; retrait par l'API | `RoleLivreurTests` |
| D4 | Transitions libres de l'administration (sortie de « livrée » après ouverture du reversement) | L'administration suit la table ; « livrée » et « annulée » définitifs | `TransitionsAdministrationTests` |
| D5 | Fiche d'une commande annulée restée « en attente », coordonnées visibles, modifiable | Statut `annulee` dans la transaction d'annulation ; migration des fiches existantes | `AnnulationTests` |
| D6 | « Échouée » sans issue (commande et reversement bloqués) | Nouvelle tentative (3 au plus) ou abandon → annulation et remboursement | `EchecTests` |
| D7 | Le vendeur ne voyait aucune livraison | Lecture seule de sa boutique, sans code | `IsolationEtSuiviTests` |
| D8 | Historique : 200 vide pour un tiers, `effectue_par` exposé au client | 404 ; rôle à la place de l'identifiant | `IsolationEtSuiviTests` |
| D9 | Suivi pauvre (id du livreur, adresse d'une ligne, aucune estimation) | Prénom, téléphone pendant la tournée, adresse structurée, date estimée | `IsolationEtSuiviTests` |
| D10 | Coordonnées du client conservées par le livreur après la livraison | Masquées une fois livrée ou annulée | `IsolationEtSuiviTests` |
| D12 | N+1 sur la liste (`commande.groupe`) | `select_related` complet, nombre de requêtes constant | `PerformanceEtLimitesTests` |
| D14 | Pas de limite dédiée | `livraison_statut` 120/h, `livraison_contestation` 10/h | `PerformanceEtLimitesTests` |
| D15 | « Livrée » et « échouée » simultanées : livraison échouée, commande livrée, reversement ouvert | Transitions sous `select_for_update` ; la seconde requête voit le nouvel état (400) | `ConcurrenceTests` |

Vérifié et correct dès le diagnostic : `is_staff` sans rôle admin n'a aucun pouvoir (API et Django admin), un livreur désactivé est refusé par le JWT (401).

## 12. Limites de débit et performance

- `livraison_statut` : 120/heure par compte (un livreur fait 4 changements par colis) ; en plus, 5 essais par code.
- `livraison_contestation` : 10/heure par compte.
- Listes paginées (20) ; `select_related("commande__groupe", "livreur", "contestation")` : nombre de requêtes constant quel que soit le nombre de livraisons (testé pour les quatre rôles).
- Ordre des verrous : fiche de livraison, puis commande, pour l'annulation comme pour les transitions (pas d'interblocage entre une annulation et une expédition simultanées).
- La vérification du code (`check_password`, PBKDF2 en production) coûte quelques dizaines de millisecondes par essai, bornée par les 5 essais et la limite de débit.

## 13. Migrations

- **livraison 0002** : `annulee`, `date_livraison_estimee`, `tentatives`, `code_hash`, `code_chiffre`, `code_essais`, `LivraisonHistorique.role_acteur`, `ContestationLivraison`.
- **livraison 0003** (données) : fiches des commandes déjà annulées (hors livrées) → `annulee`, avec une ligne d'historique `systeme`.
- **commandes 0007** : motif `livraison_echouee`.
- **paiements 0005** : motif de remboursement `livraison_non_recue`.
- **Frais de livraison (§ 9)** : **livraison 0004** (`TarifLivraison` + tarifs initiaux provisoires), **commandes 0008** (`Commande.frais_livraison`, `livraison_offerte`, `frais_livraison_vendeur` ; `GroupeCommande.livraison_zone`), **vendeurs 0005** (`Boutique.livraison_offerte`), **livraison 0005** (communes du district d'Abidjan, une zone par commune, un défaut par zone), **retours 0004** (`DemandeRetour.frais_livraison_rembourses`), **paiements 0006** (`Reversement.montant_livraison` ; `AjustementVendeur.nature` et `commande`, un ajustement de chaque nature par commande). Les commandes existantes gardent 0 de frais et une zone vide.
- **Point GPS du lieu de livraison (§ 5)** : **commandes 0009** (`GroupeCommande.livraison_latitude` / `livraison_longitude`, facultatifs, les deux ou aucun ; voir [`MODULE_COMMANDES.md`](./MODULE_COMMANDES.md) § 12). Aucune migration côté livraison : le point n'est pas copié dans la fiche.

En dev : `docker exec anitche-backend python manage.py migrate`, puis **redémarrer le worker Celery** (`docker restart infra-celery-worker-1`) pour qu'il connaisse la tâche d'envoi du code.

## 14. Tests

⚠️ **Toujours sur PostgreSQL.** Le Python de l'hôte n'a pas les dépendances (Celery) : lancer les tests dans le conteneur.

```bash
docker exec -e DJANGO_SETTINGS_MODULE=config.settings.ci -e DB_NAME=anitche_test anitche-backend \
  python manage.py test apps.livraison -v 2
```

`apps/livraison/tests.py` — 73 tests : parcours et isolation historiques (adaptés au nouveau contrat), code de livraison (sans code, faux code, blocage, haché et chiffré, visibilité, effacement, email), assignation (date estimée, réassignation, livraison terminée, propriétaire, non-livreur ou inactif, réservée à l'administration, donnée existante, Django admin), rôle livreur (retrait immédiat, par le Django admin aussi, nomination, refus du vendeur, réservé à l'administration, compte désactivé), transitions de l'administration, échec (motif, nouvelle tentative, limite, abandon, remboursement), annulation (administration, client, annulation refusée, migration), contestation (suspension, alerte, périmètre, délai, décisions, retour clos), isolation et suivi (vendeur, historique, client, livreur, `is_staff`, Django admin), performance (N+1, limites de débit), concurrence (D15). Tests adaptés hors module : `commandes` (`MachineAEtatsTests` : les étapes passent par un livreur, avec le code) et `notifications` (transition par le service).

Suite complète : **578 tests, OK, aucun « skipped »** (26/09/2026).

**Frais de livraison (§ 9)** — 41 tests : `livraison.TarifsDeLivraisonTests` (normalisation des communes, communes du district créées par migration, zone déduite de la commune, tarif applicable, commune désactivée restée à Abidjan, une zone par commune en base, livraison offerte, grille publique, administration réservée, traçabilité, validations, pas de suppression), `commandes.FraisDeLivraisonCheckoutTests` (frais par commande, commune d'Abidjan écrite de plusieurs façons → abidjan, ville hors Abidjan déclarée « abidjan » → tarif hors Abidjan et l'inverse, ville hors Abidjan avec tarif propre, tarif inactif, montants du client ignorés, 503 sans tarif, livraison offerte, frais figés, coupon sans effet sur les frais), `commandes.SimulationDuCheckoutTests` (mêmes montants que la validation, aucune écriture, part du vendeur jamais exposée, refus, limite dédiée), `paiements.FraisDeLivraisonFinancesTests` (frais hors reversement, livraison offerte déduite et plafonnée, reste dû après livraison seulement, annulation, contestation fondée, frais rendus pour un retour imputable au vendeur une fois par commande et facturés au vendeur, motifs sans frais, points de fidélité hors frais), `retours.RetoursConcurrenceTestCase` (deux demandes simultanées : frais rendus une seule fois, PostgreSQL), `vendeurs.MaBoutiqueAPITests` (option du vendeur). Tests existants adaptés : adresses sans zone, montants attendus frais compris (`FRAIS_ABIDJAN`), tarifs recréés dans les `TransactionTestCase` (`livraison.tests.recreer_tarifs_initiaux`, comme le barème). Suite complète : **728 tests, OK, aucun « skipped »** (27/09/2026).

**Point GPS du lieu de livraison (§ 5)** — `livraison.PositionLivraisonTests`, 5 tests : client et administration le voient à tout statut ; livreur assigné pendant `en_attente`, `expediee`, `en_cours`, `echouee`, masqué (détail et liste) après `livree` et `annulee`, et après une vraie livraison avec code ; autre livreur : 404 ; vendeur : jamais (livraisons et commandes, à tout statut) ; sans point : `null`. `test_suivi_client` adapté : l'objet `adresse` contient désormais `latitude` et `longitude` (`null` pour ce checkout sans point). Suite complète : **835 tests, OK, aucun « skipped »** (27/09/2026).

Postman : dossier **10. Frais de livraison** de `postman_livraison.json` (grille publique, administration des tarifs et refus, simulation, frais figés après changement de tarif, livraison offerte jusqu'au reversement, remises en état) ; plus de `zone` dans les checkouts des collections (et un scénario « zone abidjan déclarée pour une ville de l'intérieur → tarif hors Abidjan »), montants attendus frais compris (`postman_paiements.json`, `postman_retours.json`).

Postman : `postman_livraison.json` (hors dépôt, reconstruite ; l'ancienne est archivée dans `postman_archives/`). Mise en place automatique (vendeur, produit, clients, livreur, codes via Mailpit). Parcours nominal : commande payée → préparation → nomination et assignation → expédiée → en cours → **code lu dans Mailpit, comparé à celui de l'app** → livrée. Scénarios de sécurité : IDOR, historique 404, sans code, faux code, régression par l'administration, masquage, vue vendeur, contestation, échec, nouvelle tentative, abandon et remboursement, annulation, retrait du livreur. `admin_password` à renseigner.

## 15. Dette connue

- ~~**Frais de livraison**~~ : traités (§ 9). Restent :
  - ~~**Zone déclarée par le client**~~ : corrigé, la zone est déduite de la commune par le serveur (liste des communes du district).
  - **Commune saisie librement** : une commune d'Abidjan mal nommée (« Le Plateau », un quartier comme « Riviera », « Abidjan ») n'est pas reconnue et paie le tarif hors Abidjan. Le risque est pour le client, pas pour ANITCHE ; le menu déroulant du front l'évite. Des alias (quartier → commune) seraient l'étape suivante si des erreurs apparaissent.
  - **Brofodoumé** : sous-préfecture du district d'Abidjan, mais pas commune ; hors de la liste (tarif hors Abidjan), à ajouter par l'administration si ANITCHE y livre au tarif d'Abidjan.
  - **Poids et volume** ignorés (forfait par colis) ; `VarianteProduit.poids_kg` reste disponible pour un tarif au poids.
  - **Frais de renvoi du colis** lors d'un retour : toujours non modélisés ([`MODULE_RETOURS.md`](./MODULE_RETOURS.md)).
  - **Reste de livraison offerte déjà déduit d'un versement, puis contestation fondée** : pas d'avoir automatique au vendeur ; journalisé (`securite`) pour une régularisation manuelle.
- **SMS du code** : email seulement au lancement (Mailpit en dev) ; brancher un fournisseur SMS (client sans email consulté).
- **KYC livreur** : aucun dossier d'identité pour les livreurs, nommés par l'administration ; à prévoir avant de faire appel à des livreurs indépendants.
- **Changement de rôle par le Django admin des utilisateurs** : le champ `role` y reste libre (module `utilisateurs`). Un vendeur passé livreur par ce biais ne peut toujours pas livrer sa propre commande (contrôle à chaque transition), mais la règle « jamais un vendeur » n'est garantie que par l'API de nomination.
- **Stock restitué à l'abandon** : l'abandon réutilise `annuler_commande`, qui remet le stock en vente. C'est juste si le colis revient au vendeur (hypothèse retenue) ; un colis perdu demanderait une correction manuelle du stock.
- **Livreur qui commande pour lui-même** : un compte livreur ne voit dans `/api/livraison/` que les livraisons qui lui sont assignées, pas celles de ses propres achats (cas marginal, un compte ne porte qu'un rôle).
- ~~**Emails de notification envoyés dans la transaction**~~ : corrigé, les emails partent après le commit par Celery ([`MODULE_NOTIFICATIONS.md`](./MODULE_NOTIFICATIONS.md)).
- **Preuve de livraison enrichie** (photo, géolocalisation) : non prévue ; à étudier si des contestations « non reçu » se multiplient malgré le code.
- ~~**Retours** : une demande de retour est encore acceptée avant expédition (commande confirmée)~~ : corrigé, commande livrée exigée ([`MODULE_RETOURS.md`](./MODULE_RETOURS.md)).

## 16. Justification des choix

### Pourquoi l'administration assigne (et pas le vendeur ni les livreurs eux-mêmes)

- L'argent dépend de « livrée » : si le vendeur choisissait son livreur, il pourrait s'entendre avec lui pour déclarer livré un colis jamais parti. ANITCHE garde la maîtrise de sa flotte.
- Un pôle où les livreurs se servent exposerait l'adresse et le téléphone de clients à des livreurs qui ne livreront pas leur colis.
- C'est le plus simple au lancement (peu de livreurs). Un pôle ou une assignation automatique par zone restent possibles plus tard, sans toucher au contrat de statut.

### Pourquoi un code de livraison et une contestation (options 1 + 3)

- **Le code** prouve que le client était présent à la remise : ni le livreur ni le vendeur ne peuvent déclarer livré un colis qui n'a pas été remis. C'est une pratique courante des plateformes de livraison (code donné au livreur à la remise).
- **La contestation** couvre ce que le code ne couvre pas : code communiqué trop tôt ou par téléphone, colis incomplet ou non conforme au moment de la remise. Elle reprend le mécanisme de suspension des retours et ne coûte presque rien, car l'argent n'est pas encore versé pendant les 7 jours.
- **Écartée : la confirmation du client dans l'app** comme seule preuve. Un client passif bloque le vendeur, et une confirmation automatique après X heures ramène la faille de départ.
- **Haché + chiffré** : le code se vérifie par le haché ; seule la copie chiffrée permet de le réafficher au client et de l'envoyer par email sans le faire transiter en clair par la file de tâches.
- **5 essais** : même règle que les codes OTP du projet ; un code à 6 chiffres ne se devine pas en 5 essais (1 chance sur 200 000).

### Pourquoi 3 tentatives puis l'abandon

- Trois passages « en cours » (première tentative comprise) couvrent les absences ponctuelles du client sans immobiliser un colis et le paiement du client pendant des semaines. C'est l'ordre de grandeur pratiqué par les transporteurs, qui font 2 à 3 présentations avant retour à l'expéditeur.
- L'abandon est une décision de l'administration, avec motif obligatoire : il annule la commande et crée un remboursement. Ce n'est jamais automatique, parce qu'un appel au client peut encore sauver la vente.
- Réglable sans déploiement : `LIVRAISON_TENTATIVES_MAX`.

### Pourquoi « annulée » dans la transaction de l'annulation de commande

- Une seule source de vérité : une commande annulée ne peut plus avoir de livraison active, et une livraison déjà partie **bloque** l'annulation au lieu d'être oubliée.
- Même transaction, donc pas d'état intermédiaire visible, et verrou pris sur la fiche avant la commande, dans le même ordre que les transitions de livraison.

### Pourquoi un forfait par zone et commune, figé dans la commande

- C'est le modèle le plus lisible pour le client (option « forfait par commune ou zone, type Jumia CI » de l'étude initiale) : un prix connu avant de payer, qui dépend de l'endroit où il se fait livrer. Le poids demanderait des données produit fiables que les vendeurs ne renseignent pas encore.
- La zone n'est jamais demandée au client : déclarée, elle permettait de payer le tarif d'Abidjan pour une livraison à l'intérieur. Le serveur la déduit d'une liste fermée (les communes du district) ; tout ce qui n'y est pas est hors Abidjan, le cas le plus cher : aucune erreur de saisie ne fait payer moins. La liste vit dans `TarifLivraison` (pas de modèle de plus) : ajouter une commune au district, c'est lui créer un tarif en zone Abidjan.
- Champ `zone` envoyé : **ignoré** plutôt que refusé. C'est le plus simple, les anciens appels continuent de fonctionner, et la zone réellement appliquée est renvoyée par la simulation.
- Figer le tarif dans la commande garantit que le montant payé, remboursé ou reversé ne change jamais quand l'équipe ajuste la grille.
- Un seul calcul (`calculer_checkout`) sert à la simulation et à la validation : le client ne voit jamais un montant différent de celui qu'il paie.

### Pourquoi plafonner la livraison offerte au net, et le reste seulement après la livraison

- Un reversement négatif n'a pas de sens (on ne « verse » pas une dette). Le reste est traité comme les retours après versement : un ajustement déduit des prochains reversements de la boutique.
- Tant que la commande n'est pas livrée, rien n'est dû : une annulation ne doit pas coûter au vendeur une livraison qui n'a pas eu lieu.

### Pourquoi masquer les coordonnées du client une fois la livraison terminée

- Principe de minimisation : le livreur n'a besoin de l'adresse et du téléphone que pour livrer. Après la livraison (ou l'annulation), les garder accessibles n'apporte rien et expose le client (démarchage, harcèlement).
- Le téléphone du livreur n'est montré au client que pendant la tournée, pour la même raison.

### Pourquoi le point GPS suit l'adresse, mais jamais pour le vendeur

- Le point est plus sensible que l'adresse texte : c'est l'emplacement exact d'un domicile, exploitable sans connaître le quartier. Il suit donc la règle la plus stricte déjà en place (livreur assigné, masqué après la fin) sans en créer une nouvelle.
- Le vendeur prépare le colis, il ne le livre pas : la commune, le quartier et le téléphone de livraison lui suffisent. Lui donner le point n'apporterait rien et l'exposerait à des milliers de domiciles de clients.
- Une seule source : le point vit dans `GroupeCommande` et n'est copié nulle part (ni dans le paiement ni dans la fiche de livraison). Il n'y a donc qu'un endroit à masquer, et une future anonymisation n'aura qu'une colonne à effacer.
