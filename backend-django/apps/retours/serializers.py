from decimal import Decimal
from django.db import models
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from .models import DemandeRetour, RetourItem, PhotoRetour
from apps.commandes.models import Commande, CommandeItem


class PhotoRetourSerializer(serializers.ModelSerializer):
    class Meta:
        model = PhotoRetour
        fields = ["id", "image", "date_ajout"]
        read_only_fields = ["id", "date_ajout"]

    def validate_image(self, value):
        """Limite taille et type (A04/A08) : sans ce contrôle, n'importe
        quel fichier pouvait être uploadé comme "preuve" de retour, sans
        limite de poids ni vérification qu'il s'agit bien d'une image."""
        if not value:
            return value
        if value.size > 3 * 1024 * 1024:
            raise serializers.ValidationError("L'image ne doit pas dépasser 3 Mo.")
        content_type = getattr(value, 'content_type', None)
        if content_type and content_type not in ('image/jpeg', 'image/png', 'image/webp'):
            raise serializers.ValidationError(
                "Format d'image non autorisé (JPEG, PNG ou WebP uniquement)."
            )
        return value


class RetourItemSerializer(serializers.ModelSerializer):
    nom_produit = serializers.CharField(source="commande_item.nom_produit", read_only=True)
    prix_unitaire = serializers.DecimalField(source="commande_item.prix_unitaire", max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = RetourItem
        fields = ["id", "commande_item", "nom_produit", "prix_unitaire", "quantite"]
        read_only_fields = ["id", "nom_produit", "prix_unitaire"]


class DemandeRetourSerializer(serializers.ModelSerializer):
    client_email = serializers.EmailField(source="client.email", read_only=True)
    boutique_nom = serializers.CharField(source="boutique.nom", read_only=True)
    motif_display = serializers.CharField(source="get_motif_display", read_only=True)
    type_resolution_display = serializers.CharField(source="get_type_resolution_display", read_only=True)
    statut_display = serializers.CharField(source="get_statut_display", read_only=True)
    articles = RetourItemSerializer(many=True, read_only=True)
    photos = PhotoRetourSerializer(many=True, read_only=True)

    class Meta:
        model = DemandeRetour
        fields = [
            "id",
            "numero_retour",
            "commande",
            "client",
            "client_email",
            "boutique",
            "boutique_nom",
            "motif",
            "motif_display",
            "type_resolution",
            "type_resolution_display",
            "statut",
            "statut_display",
            "description",
            "montant_remboursement",
            "reponse_vendeur",
            "articles",
            "photos",
            "date_creation",
            "date_traitement",
            "date_cloture",
        ]
        read_only_fields = fields


class ArticleRetourInputSerializer(serializers.Serializer):
    commande_item_id = serializers.UUIDField()
    quantite = serializers.IntegerField(min_value=1)


class CreerDemandeRetourSerializer(serializers.Serializer):
    commande_id = serializers.UUIDField()
    motif = serializers.ChoiceField(choices=DemandeRetour.Motif.choices, default=DemandeRetour.Motif.PRODUIT_DEFECTUEUX)
    type_resolution = serializers.ChoiceField(choices=DemandeRetour.TypeResolution.choices, default=DemandeRetour.TypeResolution.REMBOURSEMENT)
    description = serializers.CharField(min_length=10)
    articles = ArticleRetourInputSerializer(many=True)

    def validate(self, attrs):
        user = self.context["request"].user
        commande_id = attrs["commande_id"]
        articles_data = attrs["articles"]

        if not articles_data:
            raise ValidationError({"articles": "Au moins un article doit être sélectionné pour le retour."})

        try:
            commande = Commande.objects.get(id=commande_id, client=user)
        except Commande.DoesNotExist:
            raise ValidationError({"commande_id": "Commande introuvable ou non autorisée."})

        # La commande doit être livrée ou confirmée pour demander un retour
        if commande.status not in (Commande.Status.LIVREE, Commande.Status.EXPEDIEE, Commande.Status.CONFIRMEE):
            raise ValidationError({"commande_id": "Impossible de demander un retour pour une commande non confirmée ou annulée."})

        items_map = {item.id: item for item in commande.article.all()}
        montant_total = Decimal("0.00")
        validated_items = []

        # Quantités déjà couvertes par des demandes de retour antérieures sur
        # ces mêmes lignes de commande, hors demandes rejetées (qui ne
        # bloquent rien puisqu'aucun retour n'a réellement eu lieu). Sans ce
        # calcul, chaque nouvelle demande n'était comparée qu'à la quantité
        # commandée initiale : un client pouvait soumettre plusieurs demandes
        # de retour distinctes sur le même article, chacune individuellement
        # sous la limite, et cumuler un remboursement/restock supérieur à ce
        # qu'il a réellement acheté (A04/A08 — intégrité des données).
        deja_retourne = {}
        anciens_items = RetourItem.objects.filter(
            commande_item_id__in=[e["commande_item_id"] for e in articles_data],
            demande_retour__commande=commande,
        ).exclude(
            demande_retour__statut=DemandeRetour.Statut.REJETE
        ).values("commande_item_id").annotate(total=models.Sum("quantite"))
        for row in anciens_items:
            deja_retourne[row["commande_item_id"]] = row["total"]

        for item_entry in articles_data:
            c_item_id = item_entry["commande_item_id"]
            qte = item_entry["quantite"]

            c_item = items_map.get(c_item_id)
            if not c_item:
                raise ValidationError({"articles": f"L'article {c_item_id} n'appartient pas à cette commande."})

            deja = deja_retourne.get(c_item_id, 0)
            if deja + qte > c_item.quantite:
                disponible = max(c_item.quantite - deja, 0)
                raise ValidationError({
                    "articles": (
                        f"Quantité de retour indisponible pour {c_item.nom_produit} : "
                        f"{disponible} restant(s) à retourner sur {c_item.quantite} commandé(s) "
                        f"({deja} déjà couvert(s) par une demande existante)."
                    )
                })

            montant_total += c_item.prix_unitaire * qte
            validated_items.append((c_item, qte))

        attrs["_commande"] = commande
        attrs["_boutique"] = commande.boutique
        attrs["_montant_remboursement"] = montant_total
        attrs["_validated_items"] = validated_items

        return attrs


class TraiterDemandeRetourSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["approuver", "rejeter", "en_transit", "receptionner", "rembourser", "cloturer"])
    reponse = serializers.CharField(required=False, allow_blank=True, default="")
    restock = serializers.BooleanField(default=True)