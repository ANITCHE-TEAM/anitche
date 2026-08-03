from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError

from .models import (
    Utilisateur,
    CodeOTP,
    TypeUsageOTP,
    DocumentKYC
)


# =====================================================
# INSCRIPTION
# =====================================================

class InscriptionSerializer(serializers.ModelSerializer):
    """
    Gère la création d'un nouveau compte utilisateur.
    """

    # Le mot de passe est uniquement accepté en écriture.
    password = serializers.CharField(write_only=True)

    class Meta:
        model = Utilisateur
        fields = [
            'email',
            'password',
            'nom',
            'prenom',
            'telephone'
        ]

    def validate_password(self, value):
        """
        Vérifie que le mot de passe respecte
        les règles définies dans AUTH_PASSWORD_VALIDATORS.
        """
        try:
            validate_password(value)
        except DjangoValidationError as erreur:
            raise serializers.ValidationError(list(erreur.messages))
        return value

    def validate_email(self, value):
        """
        Empêche la création de plusieurs comptes
        avec la même adresse email.
        """
        if Utilisateur.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(
                "Un compte existe déjà avec cet email."
            )
        return value

    def create(self, validated_data):
        """
        Utilise le manager personnalisé afin de
        hasher automatiquement le mot de passe.
        """
        return Utilisateur.objects.create_user(**validated_data)


# =====================================================
# PROFIL
# =====================================================

class ProfilSerializer(serializers.ModelSerializer):
    """
    Retourne les informations publiques du profil.
    """

    # Ces champs ne peuvent pas être modifiés via cette API.
    role = serializers.CharField(read_only=True)
    statut_kyc = serializers.CharField(read_only=True)

    class Meta:
        model = Utilisateur
        fields = [
            'nom',
            'prenom',
            'role',
            'statut_kyc'
        ]


# =====================================================
# CHANGEMENT D'EMAIL / TÉLÉPHONE
# =====================================================

class DemandeChangementContactSerializer(serializers.Serializer):
    """
    Valide une demande de modification
    d'email ou de numéro de téléphone.
    """

    nouvel_email = serializers.EmailField(required=False)
    nouveau_telephone = serializers.CharField(required=False)

    def validate(self, data):
        """
        Une seule modification est autorisée
        par demande.
        """

        if not data.get('nouvel_email') and not data.get('nouveau_telephone'):
            raise serializers.ValidationError(
                "Il faut fournir soit un nouvel email, soit un nouveau téléphone."
            )

        if data.get('nouvel_email') and data.get('nouveau_telephone'):
            raise serializers.ValidationError(
                "Une seule modification à la fois : email OU téléphone."
            )

        return data


# =====================================================
# VERIFICATION OTP
# =====================================================

class VerificationOTPSerializer(serializers.Serializer):
    """
    Reçoit un code OTP et son contexte d'utilisation.
    """

    code = serializers.CharField(max_length=6)

    # Vérifie que le type d'OTP fait partie des valeurs autorisées.
    type_usage = serializers.ChoiceField(
        choices=TypeUsageOTP.choices
    )


# =====================================================
# DOSSIER KYC
# =====================================================

class DocumentKYCSerializer(serializers.ModelSerializer):
    """
    Création d'un dossier de vérification d'identité.
    """

    class Meta:
        model = DocumentKYC
        fields = [
            'piece_identite',
            'selfie',
            'numero_mobile_money',
            'adresse',
            'compte_bancaire',
        ]

    def create(self, validated_data):
        """
        Un utilisateur ne peut posséder
        qu'un seul dossier KYC.
        """

        utilisateur = self.context['request'].user

        if hasattr(utilisateur, 'dossier_kyc'):
            raise serializers.ValidationError(
                "Un dossier KYC existe déjà pour ce compte."
            )

        return DocumentKYC.objects.create(
            utilisateur=utilisateur,
            **validated_data
        )


# =====================================================
# MOT DE PASSE OUBLIÉ
# =====================================================

class DemandeMotDePasseOublieSerializer(serializers.Serializer):
    """
    Première étape :
    réception de l'adresse email afin d'envoyer un OTP.
    """

    email = serializers.EmailField()


class ConfirmationMotDePasseOublieSerializer(serializers.Serializer):
    """
    Deuxième étape :
    validation du code OTP puis définition
    d'un nouveau mot de passe.
    """

    email = serializers.EmailField()
    code = serializers.CharField(max_length=6)

    # Le nouveau mot de passe n'est jamais renvoyé dans les réponses.
    nouveau_password = serializers.CharField(write_only=True)

    def validate_nouveau_password(self, value):
        """
        Vérifie que le nouveau mot de passe
        respecte la politique de sécurité Django.
        """
        try:
            validate_password(value)
        except DjangoValidationError as erreur:
            raise serializers.ValidationError(
                list(erreur.messages)
            )

        return value