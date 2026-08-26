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
        on_delete=models.CASCADE,
        related_name="variante_article"
        )
    nom_produit = models.CharField(max_length=100)
    prix_unitaire = models.DecimalField(max_digits=12, decimal_places=2)
    quantite = models.PositiveIntegerField()

