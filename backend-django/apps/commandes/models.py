import uuid
from django.db import models
from django.conf import settings
from django.utils import timezone



class Commande(models.Model):
    class Status(models.TextChoices):
        CREEE = "creee", "Créée"
        CONFIRMEE = "confirmee", "Confirmée"
        PREPARATION = "preparation", "Préparation"
        EXPEDIEE =  "expediee", "Expédiée"
        LIVREE = "livree", "Livrée"
        ANNULEE = "annulee", "Annulée"


    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    numero_commande = models.CharField(max_length=20, unique=True, editable=False)
    groupe = models.ForeignKey(
        "GroupeCommande",
         on_delete=models.SET_NULL,
         null=True,
         blank=True,
         related_name="commandes"
         )
    
    boutique = models.ForeignKey(
        "vendeurs.Boutique",
        on_delete=models.CASCADE
        )
    
    status = models.CharField(
        max_length=20, 
        choices= Status.choices,
        default=Status.CREEE
        )
    
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete= models.CASCADE,
        related_name="commande_client"
    )

    montant_total = models.DecimalField(max_digits=12, decimal_places=2)
    # F-10 : trace, par commande (donc par boutique), le coupon appliqué au
    # panier et la part de remise qui lui revient. Un coupon s'applique au
    # panier entier (potentiellement multi-boutique) ; sa remise est donc
    # répartie proportionnellement entre les commandes générées, plutôt que
    # d'être arbitrairement portée par une seule boutique (voir
    # ValiderPanierView). Vide si aucun coupon n'a été utilisé.
    coupon_code = models.CharField(max_length=30, blank=True)
    montant_remise = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    update_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Commande de {self.client.email} — {self.created_at.strftime('%d/%m/%Y')}"

    
    # Autonumérote le le numéro de commande
    def save(self, *args, **kwargs):
        if not self.numero_commande:
            self.numero_commande = f"CMD-{timezone.now().year}-{uuid.uuid4().hex[:8].upper()}"
        super().save(*args, **kwargs)
    


class GroupeCommande(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete= models.SET_NULL,
        null=True,
        blank=True,
        related_name="groupes_commande"
    )
    created_at = models.DateTimeField(auto_now_add=True)


class CommandeItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    commande = models.ForeignKey(
        "Commande",
        on_delete=models.CASCADE,
        related_name="article"
        )
    variante = models.ForeignKey(
        "catalogue.VarianteProduit",
        # PROTECT plutôt que CASCADE : nom_produit/prix_unitaire/quantite
        # sont déjà dénormalisés ci-dessous pour figer l'historique de
        # vente, mais un CASCADE ferait quand même disparaître la ligne
        # CommandeItem (donc l'article facturé) si la variante est
        # supprimée — inacceptable pour un historique de commande/
        # facturation qui doit rester consultable indéfiniment, y compris
        # après retrait du catalogue. PROTECT empêche la suppression tant
        # qu'une commande y fait encore référence (désactiver la variante
        # via est_active reste le chemin normal, voir Produit.est_achetable).
        on_delete=models.PROTECT,
        related_name="variante_article"
        )
    nom_produit = models.CharField(max_length=100)
    prix_unitaire = models.DecimalField(max_digits=12, decimal_places=2)
    quantite = models.PositiveIntegerField()

