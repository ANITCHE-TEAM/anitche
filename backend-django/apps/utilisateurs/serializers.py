from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from .models import Utilisateur, CodeOTP, TypeUsageOTP, DocumentKYC
from django.core.exceptions import ValidationError as DjangoValidationError

class InscriptionSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = Utilisateur
        fields = ['email', 'password', 'nom', 'prenom','telephone']

    def validate_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as erreur:
            raise serializers.ValidationError(list(erreur.messages))
        return value

    def validate_email(self, value):
        if Utilisateur.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("Un compte existe déjà avec cet email.")
        return value


    def create(self, validated_data):
        return Utilisateur.objects.create_user(**validated_data)


class ProfilSerializer(serializers.ModelSerializer):
    role = serializers.CharField(read_only=True)
    statut_kyc = serializers.CharField(read_only=True)

    class Meta:
        model = Utilisateur
        fields = ['nom', 'prenom', 'role', 'statut_kyc']


class DemandeChangementContactSerializer(serializers.Serializer):
    nouvel_email = serializers.EmailField(required=False)
    nouveau_telephone = serializers.CharField(required=False)

    def validate(self, data):
        if not data.get('nouvel_email') and not data.get('nouveau_telephone'):
            raise serializers.ValidationError(
                "Il faut fournir soit un nouvel email, soit un nouveau téléphone."
            )
        if data.get('nouvel_email') and data.get('nouveau_telephone'):
            raise serializers.ValidationError(
                "Une seule modification à la fois : email OU téléphone."
            )
        return data

class VerificationOTPSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=6)
    type_usage = serializers.ChoiceField(choices=TypeUsageOTP.choices)


class DocumentKYCSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentKYC
        fields = [
            'piece_identite', 'selfie', 'numero_mobile_money',
            'adresse', 'compte_bancaire',
        ]

    def create(self, validated_data):
        utilisateur = self.context['request'].user
        if hasattr(utilisateur, 'dossier_kyc'):
            raise serializers.ValidationError(
                "Un dossier KYC existe déjà pour ce compte."
            )
        return DocumentKYC.objects.create(utilisateur=utilisateur, **validated_data)


class DemandeMotDePasseOublieSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ConfirmationMotDePasseOublieSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(max_length=6)
    nouveau_password = serializers.CharField(write_only=True)

    def validate_nouveau_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as erreur:
            raise serializers.ValidationError(list(erreur.messages))
        return value




