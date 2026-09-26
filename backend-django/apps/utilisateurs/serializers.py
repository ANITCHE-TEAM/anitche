from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction

from .models import (
    Utilisateur,
    TypeUsageOTP,
    DocumentKYC,
    TypePieceIdentite,
    StatutKYC,
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
        # Pas de téléphone à l'inscription : l'unicité du numéro révélait
        # à qui l'essayait qu'il appartient déjà à un compte. Il s'ajoute
        # ensuite via changement-contact/ (compte connecté, email vérifié).
        fields = [
            'email',
            'password',
            'nom',
            'prenom',
        ]

    def validate(self, attrs):
        # Refus explicite plutôt qu'ignoré : un client qui enverrait encore
        # le téléphone croirait l'avoir enregistré.
        if 'telephone' in self.initial_data:
            raise serializers.ValidationError({
                'telephone': (
                    "Le téléphone ne se renseigne plus à l'inscription : ajoutez-le "
                    "ensuite depuis votre compte (changement-contact/)."
                )
            })
        return attrs

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
    Returns the private profile of the authenticated user.

    'id' is read-only. It is meant for the FastAPI service (F-11: checking
    that a delivery driver publishes their own GPS position); that check
    does not exist yet in backend-fastapi.

    telephone_verifie stays False as long as no SMS provider is plugged in
    (team decision): the phone-change code is sent by email, which proves
    the account holder asked for it, not that they own the number.

    email and telephone are read-only: they can only be changed through
    changement-contact/ with OTP verification. Making them writable here
    would bypass that flow and allow account takeover.
    """

    class Meta:
        model = Utilisateur
        fields = [
            'id',
            'email',
            'nom',
            'prenom',
            'telephone',
            'email_verifie',
            'telephone_verifie',
            'role',
            'statut_kyc',
            'date_creation',
        ]
        read_only_fields = [
            'id',
            'email',
            'telephone',
            'email_verifie',
            'telephone_verifie',
            'role',
            'statut_kyc',
            'date_creation',
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

    def validate_nouvel_email(self, value):
        """
        Empêche de demander un email déjà utilisé par un autre compte.
        Sans ce contrôle, la confirmation OTP échoue plus tard avec une
        IntegrityError (contrainte unique en base), ce qui sert
        d'oracle pour deviner les emails déjà inscrits.
        """
        utilisateur_courant = self.context['request'].user

        if Utilisateur.objects.exclude(pk=utilisateur_courant.pk).filter(
            email__iexact=value
        ).exists():
            raise serializers.ValidationError(
                "Cet email est déjà utilisé par un autre compte."
            )
        return value

    def validate_nouveau_telephone(self, value):
        """Même contrôle que pour l'email, sur le numéro de téléphone."""
        utilisateur_courant = self.context['request'].user

        if Utilisateur.objects.exclude(pk=utilisateur_courant.pk).filter(
            telephone=value
        ).exists():
            raise serializers.ValidationError(
                "Ce numéro est déjà utilisé par un autre compte."
            )
        return value


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

    Règle métier (voir validate()) : la présence du verso dépend du type
    de pièce — obligatoire pour les pièces recto/verso (CNI, permis,
    carte consulaire, carte résident), refusée pour les pièces à page
    unique (passeport, attestation d'identité).
    """
    
    # Pièces où le verso est une face distincte du document physique.
    TYPES_VERSO_OBLIGATOIRE = {
        TypePieceIdentite.CNI,
        TypePieceIdentite.PERMIS,
        TypePieceIdentite.CARTE_CONSULAIRE,
        TypePieceIdentite.CARTE_RESIDENT,
    }
    # Pièces à page unique : un verso n'a pas de sens (page d'identité
    # du passeport, attestation tenant sur une seule page).
    TYPES_VERSO_REFUSE = {
        TypePieceIdentite.PASSEPORT,
        TypePieceIdentite.ATTESTATION_IDENTITE,
    }

    # Champs chiffrés en base (TextField) : la longueur métier est validée ici.
    numero_mobile_money = serializers.CharField(max_length=20)
    compte_bancaire = serializers.CharField(max_length=50, required=False, allow_null=True, allow_blank=True)

    class Meta:
        model = DocumentKYC
        fields = [
            'type_piece',
            'piece_identite_recto',
            'piece_identite_verso',
            'selfie',
            'numero_mobile_money',
            'adresse',
            'compte_bancaire',
        ]
        # write_only : ce serializer sert à RECEVOIR les fichiers à l'upload,
        # jamais à les renvoyer. Sans ça, la réponse de création contient
        # l'URL brute MEDIA_URL du fichier — un accès direct qui contourne
        # TelechargerDocumentKYCView et son contrôle d'autorisation.
        extra_kwargs = {
            'piece_identite_recto': {'write_only': True},
            'piece_identite_verso': {'write_only': True},
            'selfie': {'write_only': True},
        }

    def validate_compte_bancaire(self, value):
            """
            Normalizes an empty bank account to NULL.
    
            Multipart forms send an empty field as "" rather than omitting it,
            which would store an empty string in a nullable column. NULL is the
            single representation of "no bank account".
            """
            if value is None:
                return None
            value = value.strip()
            return value or None
    

    def validate(self, attrs):
        """
        Applique la règle recto/verso selon le type de pièce déclaré.

        Sur une création, attrs contient toujours type_piece (champ
        obligatoire, sans default) ; piece_identite_verso est absent de
        attrs s'il n'a pas été fourni (FileField non requis).
        """
        type_piece = attrs.get('type_piece')
        verso = attrs.get('piece_identite_verso')
        libelle = TypePieceIdentite(type_piece).label if type_piece else None

        if type_piece in self.TYPES_VERSO_OBLIGATOIRE and not verso:
            raise serializers.ValidationError({
                'piece_identite_verso': (
                    f"Le verso est obligatoire pour une pièce de type "
                    f"« {libelle} »."
                )
            })

        if type_piece in self.TYPES_VERSO_REFUSE and verso:
            raise serializers.ValidationError({
                'piece_identite_verso': (
                    f"Le verso n'est pas accepté pour une pièce de type "
                    f"« {libelle} » : une seule page est attendue."
                )
            })

        return attrs

    # Champs fichiers à effacer du stockage (pas seulement réassigner en
    # base) lors d'une resoumission après refus.
    CHAMPS_FICHIERS = ('piece_identite_recto', 'piece_identite_verso', 'selfie')

    def create(self, validated_data):
        """
        Un utilisateur ne peut posséder qu'un seul dossier KYC actif à la
        fois — sauf resoumission après un refus (statut_kyc='refuse'),
        qui remplace le dossier existant plutôt que d'en créer un second.

        HARMONISATION : l'upload seul suffit désormais à soumettre la
        demande vendeur, que ce soit la première fois ou une resoumission
        après refus — soumettre_demande_vendeur() est appelée dans les
        deux branches. Avant cette harmonisation, la toute première
        soumission exigeait un second appel explicite à une vue
        /demande-vendeur/ dédiée, depuis supprimée (devenue redondante) ;
        la resoumission après refus, elle, transitionnait déjà
        automatiquement.

        CONCURRENCE : verrouille la ligne Utilisateur (select_for_update)
        pour la durée de la décision. Sans ce verrou, deux requêtes
        d'upload simultanées peuvent toutes les deux constater l'absence
        de dossier avant que l'une des deux n'insère, et la seconde lève
        alors une IntegrityError (contrainte OneToOne) non gérée — 500 au
        lieu d'un refus propre. Avec le verrou, la seconde requête, une
        fois débloquée, revoit un dossier déjà créé par la première et
        prend normalement la branche « dossier déjà existant ».
        """
        request_utilisateur = self.context['request'].user

        with transaction.atomic():
            utilisateur = Utilisateur.objects.select_for_update().get(
                pk=request_utilisateur.pk
            )
            dossier_existant = DocumentKYC.objects.filter(
                utilisateur=utilisateur
            ).first()

            if dossier_existant is None:
                dossier = DocumentKYC.objects.create(
                    utilisateur=utilisateur,
                    **validated_data
                )
                utilisateur.soumettre_demande_vendeur()
                return dossier

            if utilisateur.statut_kyc != StatutKYC.REFUSE:
                messages_par_statut = {
                    StatutKYC.EN_ATTENTE: "Une demande est déjà en attente de traitement.",
                    StatutKYC.VALIDE: "Votre dossier KYC a déjà été validé.",
                }
                message = messages_par_statut.get(
                    utilisateur.statut_kyc,
                    "Un dossier KYC existe déjà pour ce compte "
                    f"(statut actuel : {utilisateur.get_statut_kyc_display()}).",
                )
                raise serializers.ValidationError(message)

            # Resoumission après refus : les anciens fichiers sont
            # remplacés, jamais conservés à côté des nouveaux. On capture
            # uniquement leur (name, storage) AVANT toute mutation — pas
            # les objets FieldFile eux-mêmes : FieldFile.delete() remet
            # aussi le champ à None sur l'INSTANCE dont il provient, ce
            # qui est fragile à manipuler depuis un callback différé sans
            # lien garanti avec l'état courant de dossier_existant. Deux
            # chaînes + une référence de storage (sans état propre,
            # réutilisable telle quelle) suffisent à la suppression et ne
            # dépendent d'aucun objet modèle vivant.
            #
            # La suppression du STOCKAGE n'a lieu qu'après le commit
            # effectif de la transaction (transaction.on_commit) : si
            # save() ou soumettre_demande_vendeur() échoue plus bas et
            # provoque un rollback, la base revient à l'ancien dossier —
            # les fichiers doivent alors encore exister sur le disque.
            anciens_fichiers = []
            for champ in self.CHAMPS_FICHIERS:
                fichier = getattr(dossier_existant, champ)
                if fichier:
                    anciens_fichiers.append((fichier.name, fichier.storage))

            for champ, valeur in validated_data.items():
                setattr(dossier_existant, champ, valeur)

            # Un verso non redonné (ex: passage cni -> passeport, où le
            # verso est refusé) doit être vidé, pas hérité de l'ancien
            # dossier — validated_data ne contient la clé que si le
            # client l'a fournie.
            if 'piece_identite_verso' not in validated_data:
                dossier_existant.piece_identite_verso = None

            dossier_existant.commentaire_admin = None
            dossier_existant.date_traitement = None
            dossier_existant.save()

            def _supprimer_anciens_fichiers(fichiers=anciens_fichiers):
                for nom, storage in fichiers:
                    storage.delete(nom)

            transaction.on_commit(_supprimer_anciens_fichiers)

            # Même transition que pour la branche « nouveau dossier »
            # ci-dessus — cohérence garantie par StatutsKYCImpossibles
            # (ne peut pas lever ici : on vient de vérifier
            # statut_kyc == REFUSE, ni EN_ATTENTE ni VALIDE).
            utilisateur.soumettre_demande_vendeur()

            return dossier_existant


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


# =====================================================
# CONNEXION GOOGLE
# =====================================================

class ConnexionGoogleSerializer(serializers.Serializer):
    """Reçoit l'ID token émis par Google Identity Services côté frontend."""
    id_token = serializers.CharField()