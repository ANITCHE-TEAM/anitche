from django.db import transaction
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError

from .models import PasseportProduit
from apps.catalogue.models import Produit, VarianteProduit
from apps.vendeurs.permissions import ROLES_ADMINISTRATION

STATUT_PASSEPORT_VALIDE = "valide"
STATUT_PASSEPORT_REVOQUE = "revoque"
LIBELLES_STATUT_PASSEPORT = {
    STATUT_PASSEPORT_VALIDE: "Certificat valide",
    STATUT_PASSEPORT_REVOQUE: "Certificat révoqué",
}

# Volontairement génériques : le public ne doit pas savoir si la boutique
# est fermée, suspendue ou si son vendeur a perdu sa validation.
VENDEUR_INDISPONIBLE = "Vendeur indisponible"
MOTIF_INDISPONIBILITE = "Ce produit n'est plus proposé à la vente sur ANITCHE."

MESSAGE_LOT_DEJA_CERTIFIE = "Un passeport existe déjà pour ce lot de ce produit (et de cette variante)."
MESSAGE_CERTIFICATION_RESERVEE = (
    "Le statut « Certifié Authentique ANITCHE » est attribué uniquement par l'administration ANITCHE."
)
MESSAGE_REVOCATION_ADMINISTRATION = (
    "Ce passeport a été révoqué par l'administration ANITCHE : seule l'administration peut le réactiver."
)


def origine_desactivation(utilisateur):
    """Qui agit sur l'activation d'un passeport : administration ou vendeur."""
    if utilisateur.role in ROLES_ADMINISTRATION:
        return PasseportProduit.OrigineDesactivation.ADMINISTRATION
    return PasseportProduit.OrigineDesactivation.VENDEUR


def valider_statut_certification(statut, utilisateur, statut_actuel=None):
    """Seule l'administration attribue « Certifié Authentique ANITCHE ».

    Un passeport qui l'a déjà peut être modifié par son vendeur sans le
    perdre (renvoi de la même valeur, ex. PUT complet).
    """
    if (
        statut == PasseportProduit.StatutCertification.CERTIFIE_AUTHENTIQUE
        and statut != statut_actuel
        and utilisateur.role not in ROLES_ADMINISTRATION
    ):
        raise ValidationError({"statut_certification": MESSAGE_CERTIFICATION_RESERVEE})


def valider_lot_disponible(produit, variante, numero_lot, passeport_pk=None):
    """Un passeport = un lot. Vérification lisible ; la contrainte
    `passeport_unique_par_lot` reste le filet de sécurité en cas de course."""
    if not numero_lot:
        return
    existants = PasseportProduit.objects.filter(produit=produit, variante=variante, numero_lot=numero_lot)
    if passeport_pk is not None:
        existants = existants.exclude(pk=passeport_pk)
    if existants.exists():
        raise ValidationError({"numero_lot": MESSAGE_LOT_DEJA_CERTIFIE})


class PasseportPublicSerializer(serializers.ModelSerializer):
    """Certificat d'un passeport actif, vu par le public.

    Quand l'article n'est plus vendable, le certificat reste affiché (il
    atteste la fabrication), mais la boutique est masquée et aucun lien vers
    la fiche produit n'est donné.
    """

    statut_passeport = serializers.SerializerMethodField()
    statut_passeport_display = serializers.SerializerMethodField()
    produit_nom = serializers.CharField(source="produit.nom", read_only=True)
    produit_slug = serializers.SerializerMethodField()
    boutique_nom = serializers.SerializerMethodField()
    variante_nom = serializers.SerializerMethodField()
    statut_certification_display = serializers.CharField(source="get_statut_certification_display", read_only=True)
    url_verification_publique = serializers.CharField(read_only=True)
    disponible_a_la_vente = serializers.BooleanField(source="est_disponible_a_la_vente", read_only=True)
    motif_indisponibilite = serializers.SerializerMethodField()

    class Meta:
        model = PasseportProduit
        fields = [
            "code_passeport",
            "statut_passeport",
            "statut_passeport_display",
            "produit_nom",
            "produit_slug",
            "boutique_nom",
            "variante_nom",
            "numero_lot",
            "origine_geographique",
            "materiaux_utilises",
            "date_fabrication",
            "artisan_createur",
            "statut_certification",
            "statut_certification_display",
            "nb_scans",
            "url_verification_publique",
            "disponible_a_la_vente",
            "motif_indisponibilite",
        ]
        read_only_fields = fields

    def get_statut_passeport(self, obj):
        return STATUT_PASSEPORT_VALIDE

    def get_statut_passeport_display(self, obj):
        return LIBELLES_STATUT_PASSEPORT[STATUT_PASSEPORT_VALIDE]

    def get_produit_slug(self, obj):
        return obj.produit.slug if obj.est_disponible_a_la_vente else None

    def get_boutique_nom(self, obj):
        return obj.boutique.nom if obj.est_disponible_a_la_vente else VENDEUR_INDISPONIBLE

    def get_variante_nom(self, obj):
        return obj.variante.nom if obj.variante else None

    def get_motif_indisponibilite(self, obj):
        return None if obj.est_disponible_a_la_vente else MOTIF_INDISPONIBILITE


class PasseportRevoqueSerializer(serializers.ModelSerializer):
    """Passeport désactivé (révoqué) : distinct d'un code inconnu (404),
    mais le certificat n'atteste plus rien, donc aucun détail n'est donné."""

    statut_passeport = serializers.SerializerMethodField()
    statut_passeport_display = serializers.SerializerMethodField()
    disponible_a_la_vente = serializers.SerializerMethodField()

    class Meta:
        model = PasseportProduit
        fields = ["code_passeport", "statut_passeport", "statut_passeport_display", "disponible_a_la_vente"]
        read_only_fields = fields

    def get_statut_passeport(self, obj):
        return STATUT_PASSEPORT_REVOQUE

    def get_statut_passeport_display(self, obj):
        return LIBELLES_STATUT_PASSEPORT[STATUT_PASSEPORT_REVOQUE]

    def get_disponible_a_la_vente(self, obj):
        return False


class PasseportVendeurSerializer(serializers.ModelSerializer):
    produit_nom = serializers.CharField(source="produit.nom", read_only=True)
    statut_certification_display = serializers.CharField(source="get_statut_certification_display", read_only=True)
    url_verification_publique = serializers.CharField(read_only=True)

    class Meta:
        model = PasseportProduit
        fields = [
            "id",
            "code_passeport",
            "produit",
            "produit_nom",
            "variante",
            "boutique",
            "numero_lot",
            "origine_geographique",
            "materiaux_utilises",
            "date_fabrication",
            "artisan_createur",
            "statut_certification",
            "statut_certification_display",
            "nb_scans",
            "dernier_scan",
            "url_verification_publique",
            "est_actif",
            "desactive_par",
            "date_creation",
        ]
        read_only_fields = [
            "id", "code_passeport", "boutique", "nb_scans", "dernier_scan", "desactive_par", "date_creation",
        ]

    def validate(self, attrs):
        """F-21 (audit sécurité) : `produit` et `variante` restent modifiables
        (un vendeur peut corriger une erreur de saisie), mais sans validation
        d'appartenance ici, un vendeur pouvait réassigner un passeport
        existant — dont `boutique` reste le sien et donc affiché comme
        authentique — à un `produit_id` appartenant à N'IMPORTE QUEL AUTRE
        vendeur (PATCH direct, en contournant CreerPasseportSerializer.validate
        qui ne s'applique qu'à la création). Ça cassait la garantie même que
        ce module est censé apporter : un passeport de traçabilité pourrait
        certifier un produit qui n'est pas celui du vendeur affiché.
        On revalide donc ici la même règle d'appartenance qu'à la création,
        et on s'assure que la variante appartient bien au produit choisi.

        S'y ajoutent les règles de création : pas de rattachement à un
        produit ou une variante inactifs, un passeport par lot, et
        « Certifié Authentique ANITCHE » réservé à l'administration.
        """
        instance = self.instance
        produit = attrs.get("produit", getattr(instance, "produit", None))
        variante = attrs["variante"] if "variante" in attrs else getattr(instance, "variante", None)

        boutique_cible = instance.boutique if instance else None
        if produit is not None and boutique_cible is not None and produit.boutique_id != boutique_cible.id:
            raise serializers.ValidationError(
                {"produit": "Ce produit n'appartient pas à la boutique de ce passeport."}
            )

        if variante is not None and produit is not None and variante.produit_id != produit.id:
            raise serializers.ValidationError(
                {"variante": "Cette variante n'appartient pas à ce produit."}
            )

        # Seul un NOUVEAU rattachement est refusé : un produit désactivé
        # après coup n'empêche pas de corriger les autres champs.
        if "produit" in attrs and produit.pk != getattr(instance, "produit_id", None) and not produit.est_actif:
            raise serializers.ValidationError({"produit": "Ce produit est inactif."})
        if (
            "variante" in attrs
            and variante is not None
            and variante.pk != getattr(instance, "variante_id", None)
            and not variante.est_active
        ):
            raise serializers.ValidationError({"variante": "Cette variante est inactive."})

        if "statut_certification" in attrs:
            valider_statut_certification(
                attrs["statut_certification"],
                self.context["request"].user,
                statut_actuel=getattr(instance, "statut_certification", None),
            )

        valider_lot_disponible(
            produit,
            variante,
            attrs.get("numero_lot", getattr(instance, "numero_lot", "")),
            passeport_pk=getattr(instance, "pk", None),
        )
        return attrs

    def update(self, instance, validated_data):
        """N'écrit que les champs envoyés : un save() complet réécrirait
        `est_actif` avec la valeur lue en début de requête et annulerait une
        révocation faite entre-temps par l'administration. L'activation passe
        par desactiver() / reactiver(), qui enregistrent qui a désactivé.
        Tout ou rien : une réactivation refusée n'applique aucun autre champ.
        """
        est_actif = validated_data.pop("est_actif", None)
        origine = origine_desactivation(self.context["request"].user)
        with transaction.atomic():
            if validated_data:
                for champ, valeur in validated_data.items():
                    setattr(instance, champ, valeur)
                instance.save(update_fields=[*validated_data, "date_mise_a_jour"])
            if est_actif is False:
                instance.desactiver(par=origine)
            elif est_actif is True and not instance.reactiver(par=origine):
                raise PermissionDenied(MESSAGE_REVOCATION_ADMINISTRATION)
        return instance


class CreerPasseportSerializer(serializers.Serializer):
    produit_id = serializers.IntegerField()
    variante_id = serializers.IntegerField(required=False, allow_null=True)
    numero_lot = serializers.CharField(required=False, allow_blank=True, max_length=60, default="")
    origine_geographique = serializers.CharField(required=False, max_length=150, default="Côte d'Ivoire")
    materiaux_utilises = serializers.CharField(required=False, allow_blank=True, default="")
    date_fabrication = serializers.DateField(required=False, allow_null=True)
    artisan_createur = serializers.CharField(required=False, allow_blank=True, max_length=150, default="")
    statut_certification = serializers.ChoiceField(
        choices=PasseportProduit.StatutCertification.choices,
        default=PasseportProduit.StatutCertification.STANDARD,
    )

    def validate(self, attrs):
        user = self.context["request"].user
        produit_id = attrs["produit_id"]

        try:
            produit = Produit.objects.select_related("boutique").get(id=produit_id)
        except Produit.DoesNotExist:
            raise ValidationError({"produit_id": "Produit introuvable."})

        # Appartenance vérifiée AVANT l'état du produit : on ne renseigne
        # pas un vendeur sur les produits d'une autre boutique.
        if produit.boutique.proprietaire_id != user.id and user.role not in ROLES_ADMINISTRATION:
            raise ValidationError({"produit_id": "Ce produit n'appartient pas à votre boutique."})

        if not produit.est_actif:
            raise ValidationError({"produit_id": "Ce produit est inactif."})

        variante = None
        variante_id = attrs.get("variante_id")
        if variante_id:
            try:
                variante = VarianteProduit.objects.get(id=variante_id, produit=produit)
            except VarianteProduit.DoesNotExist:
                raise ValidationError({"variante_id": "Cette variante n'appartient pas à ce produit."})
            if not variante.est_active:
                raise ValidationError({"variante_id": "Cette variante est inactive."})

        valider_statut_certification(attrs["statut_certification"], user)
        valider_lot_disponible(produit, variante, attrs["numero_lot"])

        attrs["_produit"] = produit
        attrs["_variante"] = variante
        attrs["_boutique"] = produit.boutique
        return attrs
