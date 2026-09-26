import uuid
from django.db import IntegrityError, models, transaction
from django.conf import settings
from django.utils import timezone

TENTATIVES_GENERATION_NUMERO = 5


def generer_numero_commande():
    """Numéro lisible : CMD-<année>-<8 caractères hexadécimaux>."""
    return f"CMD-{timezone.now().year}-{uuid.uuid4().hex[:8].upper()}"


class Commande(models.Model):
    class Status(models.TextChoices):
        CREEE = "creee", "Créée"
        CONFIRMEE = "confirmee", "Confirmée"
        PREPARATION = "preparation", "Préparation"
        EXPEDIEE =  "expediee", "Expédiée"
        LIVREE = "livree", "Livrée"
        ANNULEE = "annulee", "Annulée"

    class MotifAnnulation(models.TextChoices):
        CLIENT = "client", "Annulée par le client"
        EXPIRATION = "expiration", "Non payée dans le délai"
        ADMINISTRATION = "administration", "Annulée par l'administration"
        BOUTIQUE_INDISPONIBLE = "boutique_indisponible", "Boutique indisponible"


    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    numero_commande = models.CharField(max_length=20, unique=True, editable=False)
    groupe = models.ForeignKey(
        "GroupeCommande",
         on_delete=models.SET_NULL,
         null=True,
         blank=True,
         related_name="commandes"
         )

    # PROTECT (client et boutique) : une commande est un historique de
    # vente et de facturation, jamais effacé en cascade avec un compte ou
    # une boutique. La suppression d'un compte devra anonymiser (dette).
    boutique = models.ForeignKey(
        "vendeurs.Boutique",
        on_delete=models.PROTECT
        )

    # Ne change que par apps.commandes.services (table des transitions).
    status = models.CharField(
        max_length=20,
        choices= Status.choices,
        default=Status.CREEE
        )
    motif_annulation = models.CharField(max_length=30, choices=MotifAnnulation.choices, blank=True)

    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete= models.PROTECT,
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

    def save(self, *args, **kwargs):
        if self.numero_commande:
            return super().save(*args, **kwargs)
        # 8 caractères hexadécimaux par année : une collision devient
        # probable au-delà de quelques dizaines de milliers de commandes.
        # On retente avec un nouveau numéro plutôt que de répondre 500.
        for tentative in range(TENTATIVES_GENERATION_NUMERO):
            self.numero_commande = generer_numero_commande()
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                numero_pris = Commande.objects.filter(numero_commande=self.numero_commande).exists()
                self.numero_commande = ""
                if not numero_pris or tentative == TENTATIVES_GENERATION_NUMERO - 1:
                    raise



class GroupeCommande(models.Model):
    """Un checkout : les commandes (une par boutique) d'un même panier,
    et l'adresse de livraison saisie au moment de la validation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete= models.SET_NULL,
        null=True,
        blank=True,
        related_name="groupes_commande"
    )
    # Adresse de livraison, obligatoire à la validation du panier (vide
    # uniquement pour les groupes créés avant son introduction).
    livraison_commune = models.CharField(max_length=100, blank=True)
    livraison_quartier = models.CharField(max_length=150, blank=True)
    livraison_point_de_repere = models.TextField(blank=True)
    # Choisi par le client pour cette livraison : visible par le vendeur
    # et le livreur (jamais le téléphone du profil).
    livraison_telephone = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def a_une_adresse(self):
        return bool(self.livraison_commune and self.livraison_quartier and self.livraison_telephone)

    @property
    def adresse_livraison_texte(self):
        """Adresse sur une ligne (Paiement.adresse_livraison, Livraison)."""
        adresse = f"{self.livraison_commune}, {self.livraison_quartier}"
        if self.livraison_point_de_repere:
            adresse += f" — {self.livraison_point_de_repere}"
        return adresse[:255]


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
