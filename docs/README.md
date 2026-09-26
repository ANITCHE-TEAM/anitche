# docs/

Ce dossier centralise la documentation vivante du projet ANITCHE :

- [`GUIDE_STRUCTURE_ANITCHE.md`](./GUIDE_STRUCTURE_ANITCHE.md) — comment le repo est organisé, pourquoi, et comment démarrer après un clone.
- [`MODULE_UTILISATEURS.md`](./MODULE_UTILISATEURS.md) — comptes, JWT, connexion Google, OTP et dossier KYC : contrat des endpoints `/api/utilisateurs/`, limites de débit, dette connue.
- [`MODULE_VENDEURS.md`](./MODULE_VENDEURS.md) — cycle de vie vendeur, contrat des endpoints `/api/vendeurs/`, dépendances avec le catalogue et les commandes.
- [`MODULE_CATALOGUE.md`](./MODULE_CATALOGUE.md) — produits, variantes, stock et images : visibilité publique, espace vendeur, modération, contrat des endpoints `/api/catalogue/`.
- [`MODULE_PANIER.md`](./MODULE_PANIER.md) — panier connecté ou anonyme, article devenu indisponible, concurrence, contrat des endpoints `/api/panier/`.
- [`MODULE_PASSEPORT_QR.md`](./MODULE_PASSEPORT_QR.md) — passeports d'authenticité, vérification publique d'un QR, contrat des endpoints `/api/passeports/`, IP et limite de débit derrière Nginx/Cloudflare.
- [`PASSATION_VENDEURS.md`](./PASSATION_VENDEURS.md) — état du backend Django, ce qui est livré, ce qui reste à faire pour la personne qui reprend.
- Ajoutez ici le backlog détaillé des 18 epics, les specs fonctionnelles, les maquettes exportées, etc.

Règle simple : si un document aide l'équipe à comprendre le projet (et pas seulement une tâche ponctuelle), il vit ici plutôt que dans un chat WhatsApp ou un Google Doc perdu.
