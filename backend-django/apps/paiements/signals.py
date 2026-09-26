from django.dispatch import Signal

# Émis par apps.paiements.services.valider_paiement, dans la transaction de
# validation, quand au moins une commande vient d'être confirmée.
# Arguments : paiement, client, adresse_livraison, commandes (les commandes
# confirmées par ce paiement — jamais celles remboursées).
# Écouteurs : apps.fidelite (points), apps.notifications (client, vendeurs).
# La confirmation des commandes, la fiche de livraison et le reversement
# sont faits directement par le service, pas par un écouteur.
paiement_valide = Signal()
