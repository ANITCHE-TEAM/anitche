from rest_framework import serializers

from apps.utilisateurs.models import DocumentKYC, Utilisateur

from .models import Boutique


class BoutiquePubliqueSerializer(serializers.ModelSerializer):
    """Fiche boutique côté client — aucune donnée personnelle du vendeur."""

    class Meta:
        model = Boutique
        fields = [
            'id', 'nom', 'slug', 'description', 'logo', 'banniere',
            'ville', 'telephone_contact', 'email_contact', 'date_creation',
        ]
        read_only_fields = fields


class BoutiqueSerializer(serializers.ModelSerializer):
    """Boutique vue par son propriétaire (création et mise à jour)."""

    proprietaire_email = serializers.EmailField(source='proprietaire.email', read_only=True)
    est_publiable = serializers.BooleanField(read_only=True)

    class Meta:
        model = Boutique
        fields = [
            'id', 'nom', 'slug', 'description', 'logo', 'banniere',
            'telephone_contact', 'email_contact', 'adresse', 'ville',
            'est_active', 'est_publiable', 'proprietaire_email',
            'date_creation', 'date_mise_a_jour',
        ]
        read_only_fields = [
            'id', 'slug', 'est_publiable', 'proprietaire_email',
            'date_creation', 'date_mise_a_jour',
        ]

    def validate(self, data):
        utilisateur = self.context['request'].user
        if self.instance is None and Boutique.objects.filter(proprietaire=utilisateur).exists():
            raise serializers.ValidationError(
                "Ce compte possède déjà une boutique."
            )
        return data

    def create(self, validated_data):
        return Boutique.objects.create(
            proprietaire=self.context['request'].user, **validated_data
        )


class BoutiqueAdministrationSerializer(BoutiqueSerializer):
    """Vue back-office : mêmes champs, plus l'identité du propriétaire."""

    proprietaire_id = serializers.IntegerField(source='proprietaire.id', read_only=True)
    proprietaire_statut_kyc = serializers.CharField(
        source='proprietaire.statut_kyc', read_only=True
    )

    class Meta(BoutiqueSerializer.Meta):
        fields = BoutiqueSerializer.Meta.fields + [
            'proprietaire_id', 'proprietaire_statut_kyc',
        ]


class DossierKYCLectureSerializer(serializers.ModelSerializer):
    """Lecture seule du dossier KYC pour l'instruction d'une demande vendeur.

    Défini ici plutôt que dans utilisateurs : c'est un besoin du back-office
    vendeur, le module utilisateurs n'a pas à changer pour ça.
    """

    class Meta:
        model = DocumentKYC
        fields = [
            'piece_identite', 'selfie', 'numero_mobile_money', 'adresse',
            'compte_bancaire', 'date_soumission', 'date_traitement',
            'commentaire_admin',
        ]
        read_only_fields = fields


class DemandeVendeurSerializer(serializers.ModelSerializer):
    """Une demande vendeur en attente, telle que vue par l'administration."""

    dossier_kyc = DossierKYCLectureSerializer(read_only=True)

    class Meta:
        model = Utilisateur
        fields = [
            'id', 'email', 'telephone', 'nom', 'prenom', 'role', 'statut_kyc',
            'email_verifie', 'telephone_verifie', 'date_creation', 'dossier_kyc',
        ]
        read_only_fields = fields


class DecisionVendeurSerializer(serializers.Serializer):
    """Corps de requête d'une décision admin (validation ou refus)."""

    commentaire = serializers.CharField(required=False, allow_blank=True, default='')


class RefusVendeurSerializer(DecisionVendeurSerializer):
    """Un refus doit être motivé : le motif est renvoyé au vendeur."""

    commentaire = serializers.CharField(required=True, allow_blank=False)
