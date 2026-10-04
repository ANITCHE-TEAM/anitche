# docs/

Ce dossier centralise la documentation vivante du projet ANITCHE :

- [`GUIDE_STRUCTURE_ANITCHE.md`](./GUIDE_STRUCTURE_ANITCHE.md) — comment le repo est organisé, pourquoi, et comment démarrer après un clone.
- [`MODULE_UTILISATEURS.md`](./MODULE_UTILISATEURS.md) — comptes, JWT, connexion Google, OTP et dossier KYC : contrat des endpoints `/api/utilisateurs/`, limites de débit, dette connue.
- [`MODULE_VENDEURS.md`](./MODULE_VENDEURS.md) — cycle de vie vendeur, contrat des endpoints `/api/vendeurs/`, dépendances avec le catalogue et les commandes.
- [`MODULE_CATALOGUE.md`](./MODULE_CATALOGUE.md) — produits, variantes, stock et images : visibilité publique, espace vendeur, modération, contrat des endpoints `/api/catalogue/`.
- [`MODULE_COMMANDES.md`](./MODULE_COMMANDES.md) — checkout (adresse obligatoire), cycle de vie des commandes, annulation et expiration, espace vendeur, contrat des endpoints `/api/commandes/`.
- [`MODULE_PAIEMENTS.md`](./MODULE_PAIEMENTS.md) — paiement en ligne (fournisseur interchangeable : CinetPay, simulé), notifications sécurisées, frais vendeur, remboursements, reversements aux vendeurs, clés CinetPay, contrat des endpoints `/api/paiements/`.
- [`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md) — frais de livraison (tarifs par commune, zone déduite par le serveur, livraison offerte), assignation et rôle livreur, code de livraison, échec et abandon, annulation, contestation « non reçu », suivi par rôle, contrat des endpoints `/api/livraison/`.
- [`MODULE_PANIER.md`](./MODULE_PANIER.md) — panier connecté ou anonyme, article devenu indisponible, concurrence, contrat des endpoints `/api/panier/`.
- [`MODULE_PASSEPORT_QR.md`](./MODULE_PASSEPORT_QR.md) — passeports d'authenticité, vérification publique d'un QR, contrat des endpoints `/api/passeports/`, IP et limite de débit derrière Nginx/Cloudflare.
- [`MODULE_RETOURS.md`](./MODULE_RETOURS.md) — retours sous 7 jours après livraison, photos, traitement par la boutique, remboursement, contrat des endpoints `/api/retours/`.
- [`MODULE_FIDELITE.md`](./MODULE_FIDELITE.md) — points crédités après le délai de rétractation, conversion en coupons, contrat des endpoints `/api/fidelite/`.
- [`MODULE_NOTIFICATIONS.md`](./MODULE_NOTIFICATIONS.md) — notifications en base, emails asynchrones, préférences, contrat des endpoints `/api/notifications/`.
- [`MODULE_SUPPORT.md`](./MODULE_SUPPORT.md) — tickets, messages, pièces jointes, file de l'équipe support, contrat des endpoints `/api/support/`.
- [`GUIDE_FRONTEND.md`](./GUIDE_FRONTEND.md) — brancher le frontend sur l'API : démarrage local, Swagger, JWT, format d'erreur, pagination, 429, comptes de démo (`seed_demo`), Mailpit, types TypeScript, index des « Impact frontend ».
- [`SAUVEGARDES.md`](./SAUVEGARDES.md) — sauvegardes chiffrées hors serveur de la base et des médias (restic, chaque nuit), surveillance, restauration, exercice mensuel, reprise après sinistre, garde des clés.
- [`PASSATION_BACKEND_DJANGO.md`](./PASSATION_BACKEND_DJANGO.md) — état du backend Django, conventions transverses, tests, règles à ne pas casser, dette : pour la personne qui reprend ou rejoint le backend.
- Ajoutez ici le backlog détaillé des 18 epics, les specs fonctionnelles, les maquettes exportées, etc.

Règle simple : si un document aide l'équipe à comprendre le projet (et pas seulement une tâche ponctuelle), il vit ici plutôt que dans un chat WhatsApp ou un Google Doc perdu.
