"""Routes logiques des notifications (`Notification.lien_redirection`).

Liste fermée, définie ici seulement : chaque portail (client, vendeur,
livreur, administration) associe ces chemins à ses propres pages. Règles :
un chemin, jamais une URL ; aucun hôte ; aucune requête (`?…`) ; `""` quand
la notification ne mène nulle part. Les liens de l'administration commencent
par `/administration/`, jamais par `/admin/` : suivi tel quel sur la même
origine, `/admin/` mènerait à l'admin Django.

Toute nouvelle route s'ajoute ici, à ROUTES, et dans
docs/MODULE_NOTIFICATIONS.md.
"""

#: Gabarits de toutes les routes produites (`{uuid}` : identifiant UUID,
#: `{id}` : identifiant entier). Repris par le texte d'aide du champ.
ROUTES = (
    "/commandes",
    "/commandes/{uuid}",
    "/livraisons/{uuid}",
    "/retours/{uuid}",
    "/paiements/{uuid}",
    "/fidelite/mon-compte",
    "/support/tickets/{uuid}",
    "/vendeur/commandes/{uuid}",
    "/vendeur/produits/{id}",
    "/vendeur/retours/{uuid}",
    "/vendeur/reversements",
    "/administration/retours/{uuid}",
    "/administration/remboursements",
    "/administration/livraisons/{uuid}",
)


# --- Client ------------------------------------------------------------

def lien_commandes_client():
    return "/commandes"


def lien_commande_client(commande):
    return f"/commandes/{commande.pk}"


def lien_livraison_client(livraison):
    return f"/livraisons/{livraison.pk}"


def lien_retour_client(demande):
    return f"/retours/{demande.pk}"


def lien_paiement_client(paiement):
    return f"/paiements/{paiement.pk}"


def lien_fidelite_client():
    return "/fidelite/mon-compte"


# --- Client, vendeur et agent ------------------------------------------

def lien_ticket(ticket):
    return f"/support/tickets/{ticket.pk}"


# --- Vendeur -----------------------------------------------------------

def lien_commande_vendeur(commande):
    return f"/vendeur/commandes/{commande.pk}"


def lien_produit_vendeur(produit):
    return f"/vendeur/produits/{produit.pk}"


def lien_retour_vendeur(demande):
    return f"/vendeur/retours/{demande.pk}"


def lien_reversements_vendeur():
    return "/vendeur/reversements"


# --- Administration ----------------------------------------------------

def lien_retour_administration(demande):
    return f"/administration/retours/{demande.pk}"


def lien_remboursements_administration():
    return "/administration/remboursements"


def lien_livraison_administration(livraison):
    return f"/administration/livraisons/{livraison.pk}"
