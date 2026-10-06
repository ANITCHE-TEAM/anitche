import django.dispatch

# Émis à chaque changement de statut d'une livraison.
# Écouté (plus tard) par apps.notifications pour prévenir le client
# (ex: "Votre commande a été expédiée" / "Votre commande a été livrée").
#
# Providing args:
#   livraison        -> instance Livraison
#   ancien_status     -> ancien statut (str)
#   nouveau_status    -> nouveau statut (str)
#   effectue_par      -> Utilisateur ayant déclenché le changement (ou None)
livraison_status_change = django.dispatch.Signal()