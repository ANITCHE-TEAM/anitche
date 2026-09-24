import secrets
import logging
from django.db import models, transaction
from datetime import timedelta
from django.utils import timezone

from .managers import UtilisateurManager
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.contrib.auth.hashers import make_password, check_password
from apps.core.validators import validateur_document_kyc, validateur_image_standard
from apps.core.storage import CheminUploadUUID

logger_securite = logging.getLogger('securite')


# ============================
# ENUMERATIONS
# ============================

class Role(models.TextChoices):
    """Liste des rôles disponibles sur la plateforme."""

    CLIENT = 'client', 'Client'
    VENDEUR = 'vendeur', 'Vendeur'
    LIVREUR = 'livreur', 'Livreur'
    MODERATEUR = 'moderateur', 'Modérateur'
    SUPPORT = 'support', 'Service Client'
    ADMIN = 'admin', 'Administrateur'
    SUPER_ADMIN = 'super_admin', 'Super Administrateur'


class StatutKYC(models.TextChoices):
    """États possibles d'une demande de vérification d'identité."""

    NON_SOUMIS = 'non_soumis', 'Non soumis'
    EN_ATTENTE = 'en_attente', 'En attente'
    VALIDE = 'valide', 'Validé'
    REFUSE = 'refuse', 'Refusé'


class StatutsKYCImpossibles(Exception):
    """Exception levée lorsqu'une transition KYC est interdite."""
    pass


# ============================
# UTILISATEUR
# ============================

class Utilisateur(AbstractBaseUser, PermissionsMixin):
    """
    Modèle utilisateur personnalisé.
    L'email est utilisé comme identifiant principal de connexion.
    """

    email = models.EmailField(unique=True)
    google_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        unique=True,
        help_text="Identifiant unique Google (sub), rempli si le compte est lié à Google.",
    )

    telephone = models.CharField(max_length=20, unique=True, null=True, blank=True)

    nom = models.CharField(max_length=100)
    prenom = models.CharField(max_length=100)

    # Détermine les permissions fonctionnelles de l'utilisateur.
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.CLIENT,
    )

    # Représente l'état actuel de la vérification d'identité.
    statut_kyc = models.CharField(
        max_length=20,
        choices=StatutKYC.choices,
        default=StatutKYC.NON_SOUMIS,
    )

    # Vérification des moyens de contact.
    email_verifie = models.BooleanField(default=False)
    telephone_verifie = models.BooleanField(default=False)

    # Champs requis par le système d'authentification Django.
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    # Dates de suivi.
    date_creation = models.DateTimeField(auto_now_add=True)
    date_mise_a_jour = models.DateTimeField(auto_now=True)

    objects = UtilisateurManager()

    # L'utilisateur se connecte avec son adresse email.
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['nom', 'prenom']

    def soumettre_demande_vendeur(self):
        """
        Passe le dossier KYC en attente de validation.

        Impossible si une demande est déjà en cours
        ou si le vendeur est déjà validé.
        """
        if self.statut_kyc in [StatutKYC.EN_ATTENTE, StatutKYC.VALIDE]:
            raise StatutsKYCImpossibles(
                "Une demande est déjà en cours ou déjà validée."
            )

        self.statut_kyc = StatutKYC.EN_ATTENTE
        self.save(update_fields=['statut_kyc'])

    def __str__(self):
        return f"{self.email} ({self.get_role_display()})"

    class Meta:
        verbose_name = "Utilisateur"
        verbose_name_plural = "Utilisateurs"


# ============================
# DOSSIER KYC
# ============================

class TypePieceIdentite(models.TextChoices):
    """
    Types de pièce d'identité acceptés pour le KYC.

    Détermine si un verso est obligatoire, refusé ou sans objet
    (voir DocumentKYCSerializer.validate) :
    - recto/verso obligatoires : CNI, permis, carte consulaire, carte résident ;
    - une seule page (verso refusé) : passeport (page d'identité), attestation
      d'identité.
    """

    CNI = 'cni', "Carte nationale d'identité"
    PASSEPORT = 'passeport', 'Passeport'
    PERMIS = 'permis', 'Permis de conduire'
    CARTE_CONSULAIRE = 'carte_consulaire', 'Carte consulaire'
    CARTE_RESIDENT = 'carte_resident', 'Carte de résident'
    ATTESTATION_IDENTITE = 'attestation_identite', "Attestation d'identité"


class DocumentKYC(models.Model):
    """
    Ensemble des documents transmis par un utilisateur
    pour obtenir le statut de vendeur.
    """

    utilisateur = models.OneToOneField(
        Utilisateur,
        on_delete=models.CASCADE,
        related_name='dossier_kyc',
    )

    # Détermine, via DocumentKYCSerializer, si piece_identite_verso est
    # obligatoire, refusé ou sans objet. Pas de default : la valeur doit
    # être explicitement fournie par le client à l'upload (voir la
    # migration pour la valeur de bascule des dossiers déjà existants).
    type_piece = models.CharField(
        max_length=30,
        choices=TypePieceIdentite.choices,
    )

    # Documents nécessaires à la vérification. Le verso reste optionnel
    # au niveau du modèle (pas toutes les pièces n'en ont un) — c'est
    # DocumentKYCSerializer.validate qui applique la règle réelle,
    # conditionnelle à type_piece.
    # upload_to en UUID (voir apps.core.storage.CheminUploadUUID) : le nom
    # d'origine du fichier n'est jamais conservé, ni comme nom de fichier
    # stocké, ni comme indice dans le chemin — chaque face a son propre
    # sous-dossier.
    piece_identite_recto = models.FileField(
        upload_to=CheminUploadUUID('kyc/pieces_identite/recto'),
        validators=[validateur_document_kyc],
    )
    piece_identite_verso = models.FileField(
        upload_to=CheminUploadUUID('kyc/pieces_identite/verso'),
        validators=[validateur_document_kyc],
        null=True,
        blank=True,
    )
    selfie = models.ImageField(
        upload_to=CheminUploadUUID('kyc/selfies'),
        validators=[validateur_image_standard],
    )

    numero_mobile_money = models.CharField(max_length=20)
    adresse = models.TextField()

    # Facultatif selon le mode de paiement.
    compte_bancaire = models.CharField(max_length=50, null=True, blank=True)

    date_soumission = models.DateTimeField(auto_now_add=True)
    date_traitement = models.DateTimeField(null=True, blank=True)

    # Commentaire laissé par l'administrateur après étude.
    commentaire_admin = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"KYC de {self.utilisateur.email}"

    class Meta:
        verbose_name = "Dossier KYC"
        verbose_name_plural = "Dossiers KYC"


# ============================
# OTP
# ============================

class TypeUsageOTP(models.TextChoices):
    """Cas d'utilisation des codes OTP."""

    INSCRIPTION = 'inscription', 'Inscription'
    MOT_DE_PASSE_OUBLIE = 'mdp_oublie', 'Mot de passe oublié'
    CHANGEMENT_EMAIL = 'changement_email', 'Changement email'
    CHANGEMENT_TELEPHONE = 'changement_telephone', 'Changement téléphone'


class CodeOTP(models.Model):
    """
    Stocke un code OTP sécurisé.

    Le code n'est jamais enregistré en clair :
    seul son hash est conservé en base de données.
    """

    utilisateur = models.ForeignKey(
        Utilisateur,
        on_delete=models.CASCADE,
        related_name='codes_otp',
    )

    # Hash du code OTP.
    code_hash = models.CharField(max_length=128)

    type_usage = models.CharField(
        max_length=30,
        choices=TypeUsageOTP.choices
    )

    # Utilisé lors d'un changement d'email ou de téléphone.
    nouvelle_valeur = models.CharField(
        max_length=255,
        null=True,
        blank=True
    )

    date_creation = models.DateTimeField(auto_now_add=True)
    date_expiration = models.DateTimeField()

    # Nombre de tentatives de saisie.
    nombre_tentatives = models.PositiveIntegerField(default=0)

    # Empêche la réutilisation du même OTP.
    utilise = models.BooleanField(default=False)

    # Constantes métier.
    NOMBRE_TENTATIVES_MAX = 5
    DUREE_VALIDITE_MINUTES = 10

    @classmethod
    def generer(cls, utilisateur, type_usage, nouvelle_valeur=None):
        """
        Génère un code OTP à 6 chiffres.

        Retourne :
        - l'instance enregistrée en base
        - le code en clair (à envoyer par SMS ou email)
        """

        # secrets (CSPRNG) plutôt que random (Mersenne Twister, non
        # cryptographiquement sûr) : un code OTP prévisible casse toute
        # la protection qu'il est censé apporter.
        code = f"{secrets.randbelow(1000000):06d}"

        instance = cls.objects.create(
            utilisateur=utilisateur,

            # On stocke uniquement le hash du code.
            code_hash=make_password(code),

            type_usage=type_usage,
            nouvelle_valeur=nouvelle_valeur,

            date_expiration=timezone.now() +
            timedelta(minutes=cls.DUREE_VALIDITE_MINUTES),
        )

        return instance, code

    def verifier(self, code_saisi):
        """
        Vérifie la validité d'un code OTP.

        Vérifications effectuées :
        - déjà utilisé
        - expiré
        - nombre maximal de tentatives
        - correspondance avec le hash enregistré

        CONCURRENCE : verrouille la ligne (select_for_update) le temps de
        la vérification + de l'incrément de nombre_tentatives. Sans ce
        verrou, deux requêtes de vérification simultanées peuvent lire la
        même valeur de nombre_tentatives avant que l'une des deux
        n'enregistre son incrément, ce qui permet de dépasser
        NOMBRE_TENTATIVES_MAX d'une unité (perte de mise à jour classique
        en lecture-puis-écriture concurrente).
        """

        with transaction.atomic():
            otp = CodeOTP.objects.select_for_update().get(pk=self.pk)

            if otp.utilise:
                return False, "Ce code a déjà été utilisé."

            if timezone.now() > otp.date_expiration:
                return False, "Ce code a expiré."

            if otp.nombre_tentatives >= self.NOMBRE_TENTATIVES_MAX:
                logger_securite.warning(
                    "Verrouillage OTP : nombre maximal de tentatives atteint "
                    "(otp_id=%s, utilisateur_id=%s, type_usage=%s)",
                    otp.id, otp.utilisateur_id, otp.type_usage,
                )
                return False, "Nombre maximal de tentatives atteint."

            # Chaque tentative est comptabilisée.
            otp.nombre_tentatives += 1
            otp.save(update_fields=['nombre_tentatives'])

            if check_password(code_saisi, otp.code_hash):
                otp.utilise = True
                otp.save(update_fields=['utilise'])

                return True, "Code valide."

            return False, "Code incorrect."

    def __str__(self):
        return f"OTP {self.type_usage} — {self.utilisateur.email}"

    class Meta:
        verbose_name = "Code OTP"
        verbose_name_plural = "Codes OTP"