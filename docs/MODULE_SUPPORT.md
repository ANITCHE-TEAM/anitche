# Module support — contrat et règles

> Périmètre : backend Django, `backend-django/apps/support/` (identifiants en anglais dans ce module, convention d'origine conservée) et ses points de contact dans `commandes`, `catalogue`, `notifications`.
> Principe : un ticket est souvent la trace d'un **litige** : il doit rester complet, et n'être lu que par les personnes concernées.

## 1. Rôles

| Rôle | Voit | Peut |
|---|---|---|
| Client (ou livreur) | ses tickets | créer, écrire, joindre des fichiers à ses messages, fermer son ticket, le noter une fois résolu |
| Vendeur | ses tickets **et** les litiges visant sa boutique (catégories `product`, `vendor_dispute`) | idem ; répondre sur un litige de sa boutique |
| Agent `support` | la file (tickets non assignés) et ses tickets | prendre un ticket, statuts, reclassement, notes internes, réponses ; le premier agent qui agit sur un ticket de la file le prend |
| Administration (`admin`, `super_admin`) | tout | tout, dont la réassignation |

`is_staff` seul ne donne aucun pouvoir métier : tout passe par le rôle.

## 2. Architecture

```text
services.py     visibilité (get_visible_tickets), table des transitions, assignation,
                messages (statut automatique, notes internes), contrôles des pièces jointes,
                notifications
views.py        HTTP, limites de débit à la création, téléchargement des pièces jointes
serializers.py  lien commande / produit acheté (création seulement), représentations
```

## 3. Statuts

```text
open ─► in_progress ⇄ waiting_customer ─► resolved ─► closed (définitif)
  └──────────┴──────────────┴──────────────────┴─────► closed
```

- Transitions du staff selon cette table (**400** sinon) ; le créateur ne peut que **fermer** son ticket.
- Première réponse du staff sur un ticket `open` → `in_progress`. Réponse du client sur `waiting_customer` ou `resolved` → `in_progress`.
- `closed` : plus de message ni de pièce jointe (ouvrir un nouveau ticket).
- Note de satisfaction (1 à 5) : une fois, par le créateur, sur un ticket `resolved` ou `closed`.
- Toutes les modifications se font sous verrou du ticket.

## 4. Endpoints — `/api/support/`

| Méthode | Chemin | Détail |
|---|---|---|
| GET / POST | `tickets/` | Liste (visibilité § 1), création. Corps : `subject`, `description`, `category`, `priority?`, **`order?`** (une commande du demandeur), **`product?`** (un produit qu'il a acheté, dans `order` si elle est donnée). `vendor` est déduit (boutique du produit ou de la commande) pour `product` et `vendor_dispute` seulement. **400** : commande ou produit d'autrui (même message qu'inexistant). **429** : `support_ticket` |
| GET / PATCH | `tickets/<uuid>/` | Détail ; PATCH `category` / `priority` par le staff (**403** sinon). `subject`, `description`, `order`, `product` immuables. **DELETE → 405** |
| PATCH | `tickets/<uuid>/status/` | `{"status"}` : § 3 |
| POST | `tickets/<uuid>/assign/` | **Nouveau.** Agent : prendre le ticket (corps vide). Administration : `{"assigned_to": <id>}` (agent support ou administrateur actif) ou soi-même. **403** : client, agent qui assigne à un autre |
| PATCH | `tickets/<uuid>/rate/` | `{"satisfaction_rating": 1..5}` |
| GET / POST | `tickets/<uuid>/messages/` | Fil (paginé ; notes internes réservées au staff) ; la lecture marque `read_at` sur les messages des autres. POST `{"content", "is_internal_note?"}` (retenu pour le staff seulement). **400** : ticket fermé. **429** : `support_message` |
| GET / POST | `messages/<uuid>/attachments/` | Liste ; ajout multipart `file` par **l'auteur du message** seulement (**403** sinon), 5 par message, ticket non fermé ; **404** sur une note interne pour un non-staff. **429** : `support_piece_jointe` |
| GET | `attachments/<uuid>/` | **Nouveau.** Le fichier, après contrôle d'accès (mêmes règles que le message) ; **401** sans jeton |

Pièce jointe : `id`, `message`, **`file` = URL de téléchargement authentifiée**, `file_type` (**calculé** : `image` pour JPEG/PNG/WebP, sinon `document`), `original_filename`, `file_size`, `created_at`. Fichiers : JPEG, PNG, WebP, PDF, 10 Mo, contenu réel vérifié, stockés sous un nom UUID (`support/pieces_jointes/`).

## 5. Cohérence avec les contestations et les retours

Trois circuits, trois besoins :

| Besoin du client | Circuit | Pourquoi |
|---|---|---|
| « Je n'ai pas reçu mon colis » (commande livrée, 7 jours) | **Contestation de livraison** (`POST /api/livraison/<id>/contester/`) | Suspend le reversement et déclenche une décision de l'administration |
| « Je veux renvoyer un article » (commande livrée, 7 jours) | **Retour** (`POST /api/retours/`) | Quantités, photos, remboursement et part du vendeur calculés |
| Question, problème de compte, de paiement, recours après un rejet de retour ou de contestation, hors délai | **Ticket support**, lié à la commande | Échange libre avec le support, traçable |

Le frontend oriente le client : sur une commande livrée dans le délai, les boutons « Je n'ai pas reçu mon colis » et « Retourner un article » viennent avant « Contacter le support ». Le backend ne bloque pas un ticket sur ces commandes : un recours doit toujours rester possible.

## 6. Impact frontend

1. **Création** : proposer de lier le ticket à une commande (`order`) et à un produit de cette commande (`product`). La réponse contient `order`, `product`, `vendor`.
2. **Orientation** (§ 5) avant le formulaire de ticket.
3. **Pièces jointes** : `file` est une **URL d'API authentifiée** (pas un chemin `/media/…`) ; la charger avec le jeton. Ne plus envoyer `file_type` (calculé). Le bouton « joindre » n'apparaît que sur ses propres messages, 5 au plus.
4. **Suppression** : retirer tout bouton « Supprimer » (**405**).
5. **Statuts** : ne proposer que les transitions de § 3 ; « Fermé » est définitif ; masquer la saisie de message sur un ticket fermé. Noter seulement quand le ticket est résolu ou fermé.
6. **Back-office support** : file des tickets non assignés, bouton « Prendre en charge » (`assign/`), réassignation par l'administration, case « note interne » (`is_internal_note`).
7. **Messages** : `read_at` est renseigné (« Lu à … »).
8. **Erreurs à prévoir** : **429** au-delà de 10 tickets, 60 messages ou 20 pièces jointes par heure.
9. **Format d'erreur unifié (septembre 2026).** Les refus suivent le format commun `{success: false, status_code, detail, errors}`, `errors` toujours un objet clé → liste de messages (`{}` si aucun champ n'est en cause). Note hors de 1 à 5 ou non entière → **400** `errors.satisfaction_rating` ; ticket pas encore résolu ou déjà noté → **400** (message dans `detail`) ; note par un autre que le créateur → **403**.

## 7. Sécurité — risques couverts

Chaque risque a son test permanent dans `apps/support/tests.py`.

| Risque | Protection | Test |
|---|---|---|
| Tests (dont les notes internes) jamais exécutés (classe imbriquée) | Classe au niveau du module | `TestsCollectesTests` |
| Ticket impossible à lier à une commande ou un produit, ou lié à ceux d'autrui | `order` / `product` vérifiés (siens, achetés) | `TicketLieACommandeTests` |
| Note interne écrite par le client, ou impossible pour le staff | `is_internal_note` réservé au staff | `NotesInternesTests` |
| Un client liste et ajoute des pièces jointes sur une note interne ou le message d'un autre ; `file_type` au choix du client ; nom d'origine sur disque ; `/media/` public en dev, absent en production | Auteur seulement, notes internes invisibles, type calculé, UUID, téléchargement authentifié, 5 par message | `PiecesJointesTests` |
| Toutes les transitions permises (fermé → ouvert) ; un agent agissant sur tout ticket non assigné sans le prendre ; aucune assignation par l'API | Table de transitions, prise en charge automatique, `assign/` | `StatutsEtAssignationTests` |
| Suppression définitive par le staff (y compris un agent) | **405** pour tous | `SupportTicketTestCase` |
| Messages sur un ticket fermé, note avant résolution, `read_at` jamais renseigné | Refus, lecture marquée | `FilDeDiscussionTests`, `StatutsEtAssignationTests` |
| Aucune limite dédiée, aucune notification | Limites dédiées ; notifications (nouveau ticket, réponses) | `LimitesDeDebitTests`, `FilDeDiscussionTests` |

Également vérifié : isolation client / vendeur sur la liste et le détail, pas de N+1 (les listes n'exposent que des identifiants), `is_staff` sans pouvoir.

## 8. Notifications

- **Nouveau ticket** : chaque agent support actif (in-app).
- **Réponse du staff** (hors note interne) et changement de statut par le staff : le créateur (in-app + email selon sa préférence).
- **Réponse du client ou du vendeur** : l'agent assigné (in-app). Un ticket de la file est déjà visible par tous les agents.

## 9. Migrations

- **support 0006** : `TicketAttachment.file` renommé en UUID (`support/pieces_jointes/`). Les fichiers existants gardent leur nom.

## 10. Tests

`apps/support/tests.py` (PostgreSQL) : tests historiques (suppression → 405), classe des messages rétablie, puis un test par faille (§ 7).

Postman : `postman_support.json` (hors dépôt, nouvelle). Parcours : commande payée → IDOR (commande d'autrui, produit non acheté) → litige produit lié à la commande (boutique déduite, visible du vendeur) → question de paiement (invisible du vendeur) → refus client (suppression 405, priorité 403, assignation 403, note trop tôt 400) → message, pièce jointe (type calculé, URL authentifiée, téléchargements 200 / 404 / 401) → réponse du vendeur, pièce jointe sur son message refusée (403) → **dossier 6 avec le compte admin** : note interne invisible du client, réponse, prise en charge, attente du client, relance par le client, résolution, note, fermeture définitive. `admin_password` à renseigner (aucun compte agent support en dev).

## 11. Dette connue

- **Nomination des agents support** : uniquement par le Django admin (champ `role`), comme les administrateurs.
- **Délai de réponse / SLA** : aucun indicateur (âge des tickets de la file) ; à ajouter avec le back-office.
- **Réouverture par le client** : un ticket fermé ne se rouvre pas ; le client en ouvre un nouveau (lié à la même commande).

## 12. Justification des choix

### Pourquoi l'agent prend le ticket en agissant

Deux agents qui répondent au même client en parallèle se contredisent. Prendre le ticket à la première action (réponse, statut, reclassement), sous verrou, garantit un seul interlocuteur sans ajouter d'étape obligatoire ; `assign/` reste là pour prendre un ticket avant d'y répondre, et pour la réassignation par l'administration.

### Pourquoi plus de suppression par l'API

Un ticket peut être la seule trace écrite d'un litige (colis, remboursement, comportement d'un vendeur). La suppression par un agent effaçait cette preuve. La suppression reste possible dans le Django admin (demande légale d'effacement, spam), par l'administration seulement.

### Pourquoi la boutique ne voit que les litiges produit ou vendeur

Lier un ticket à une commande donnait accès, via `vendor`, à toute la conversation. Une question de paiement ou de compte ne regarde pas le vendeur ; un produit défectueux ou un litige avec lui, si : il doit pouvoir répondre.

### Pourquoi garder trois circuits

La contestation et le retour déclenchent de l'argent (suspension, remboursement, part du vendeur) avec des règles strictes et testées. Les fondre dans le support ferait porter ces règles à un échange libre. Le support reste la voie des questions et des **recours**, liée à la commande pour que l'agent retrouve le contexte sans le demander.
