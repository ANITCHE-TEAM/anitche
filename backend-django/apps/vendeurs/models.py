import unicodedata

from django.conf import settings
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.db import models
from django.utils.text import slugify

from apps.utilisateurs.models import Role, StatutKYC, Utilisateur
from apps.core.validators import validateur_image_standard


def normaliser_nom_boutique(nom):
    """Forme de comparaison d'un nom de boutique.

    Insensible à la casse, aux accents, aux espaces et à la ponctuation :
    « Chez Awa », « chez-awa » et « CHÉZ  AWA ! » donnent tous `chezawa`.
    Sert uniquement à empêcher des noms trop proches (usurpation d'une
    boutique existante) ; le nom affiché reste celui saisi par le vendeur.
    """
    decompose = unicodedata.normalize('NFKD', nom.casefold())
    return ''.join(caractere for caractere in decompose if caractere.isalnum())


class BoutiqueQuerySet(models.QuerySet):
    def ouvertes(self):
        """Ni fermée par le vendeur, ni suspendue par l'administration."""
        return self.filter(est_active=True, est_suspendue=False)

    def publiques(self):
        """Boutiques visibles côté client : vendeur validé et boutique ouverte.

        Le filtre s'appuie sur `statut_kyc`, source de vérité de l'état vendeur
        (module utilisateurs) — aucun statut de validation n'est dupliqué ici.
        """
        return self.ouvertes().filter(
            proprietaire__role=Role.VENDEUR,
            proprietaire__statut_kyc=StatutKYC.VALIDE,
            proprietaire__is_active=True,
        )


class Boutique(models.Model):
    """Vitrine d'un vendeur. Une boutique par compte utilisateur validé."""

    proprietaire = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='boutique',
        verbose_name="Propriétaire",
    )

    nom = models.CharField(max_length=120, unique=True)
    # Recalculé à chaque save() depuis `nom` : jamais saisi directement.
    nom_normalise = models.CharField(max_length=120, unique=True, editable=False)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    description = models.TextField(blank=True)

    logo = models.ImageField(
        upload_to='boutiques/logos/', null=True, blank=True,
        validators=[validateur_image_standard],
    )
    banniere = models.ImageField(
        upload_to='boutiques/bannieres/', null=True, blank=True,
        validators=[validateur_image_standard],
    )

    telephone_contact = models.CharField(max_length=20, blank=True)
    email_contact = models.EmailField(blank=True)
    adresse = models.TextField(blank=True)
    ville = models.CharField(max_length=100, blank=True)

    est_active = models.BooleanField(
        default=True,
        help_text="Fermeture volontaire par le vendeur (la boutique disparaît du catalogue public).",
    )
    est_suspendue = models.BooleanField(
        default=False,
        help_text=(
            "Suspension par l'administration : la boutique disparaît du catalogue public "
            "et le vendeur ne peut pas la lever lui-même."
        ),
    )

    date_creation = models.DateTimeField(auto_now_add=True)
    date_mise_a_jour = models.DateTimeField(auto_now=True)

    objects = BoutiqueQuerySet.as_manager()

    @property
    def vendeur_est_valide(self):
        """Le compte lié est-il un vendeur validé ? (règle portée par utilisateurs)"""
        return (
            self.proprietaire.role == Role.VENDEUR
            and self.proprietaire.statut_kyc == StatutKYC.VALIDE
        )

    @property
    def est_publiable(self):
        """Autorisation de publier / d'être visible publiquement.

        Point d'entrée unique pour les autres modules (catalogue, commandes) :
        un vendeur non validé, une boutique fermée ou suspendue ne publie rien.
        `Produit.objects.publies()` (catalogue) duplique cette règle en SQL :
        toute évolution ici doit y être répercutée.
        """
        return (
            self.est_active
            and not self.est_suspendue
            and self.proprietaire.is_active
            and self.vendeur_est_valide
        )

    def clean(self):
        erreurs = {}
        # Création seulement : sur une boutique existante, l'accès du vendeur
        # est coupé en amont par EstVendeurValide (403 sur toutes les routes
        # ma-boutique/, lecture comprise) dès que son KYC n'est plus validé.
        if self._state.adding and not self.vendeur_est_valide:
            erreurs[NON_FIELD_ERRORS] = [
                "Seul un compte vendeur validé (statut_kyc = validé) peut ouvrir une boutique."
            ]
        if self.nom:
            try:
                verifier_nom_boutique_disponible(self.nom, boutique_pk=self.pk)
            except ValidationError as erreur:
                erreurs['nom'] = erreur.messages
        if erreurs:
            raise ValidationError(erreurs)

    def _generer_slug_unique(self):
        base = slugify(self.nom)[:120] or 'boutique'
        slug = base
        compteur = 2
        while Boutique.objects.filter(slug=slug).exclude(pk=self.pk).exists():
            slug = f"{base}-{compteur}"
            compteur += 1
        return slug

    def save(self, *args, **kwargs):
        self.nom_normalise = normaliser_nom_boutique(self.nom)
        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'nom' in update_fields:
            kwargs['update_fields'] = {*update_fields, 'nom_normalise'}
        if not self.slug:
            self.slug = self._generer_slug_unique()
        if self._state.adding:
            # Défense en profondeur (A04:2025) : le chemin API (DRF) ne
            # déclenche jamais clean()/full_clean() sur un ModelSerializer
            # — seul l'admin Django le fait automatiquement via ModelForm.
            # Sans cet appel explicite, la règle "vendeur validé requis à
            # la création" posée par clean() ci-dessus est aujourd'hui
            # protégée uniquement par la permission EstVendeurValide de
            # MaBoutiqueView : suffisant pour l'API actuelle, mais un
            # futur appel direct (shell, script, tâche Celery) la
            # contournerait silencieusement sans ce filet de sécurité.
            # `nom_normalise` est exclu : clean() le vérifie déjà (message
            # porté par `nom`), inutile de doubler l'erreur.
            self.full_clean(exclude=['nom_normalise'])
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.nom} ({self.proprietaire.email})"

    class Meta:
        verbose_name = "Boutique"
        verbose_name_plural = "Boutiques"
        ordering = ['nom']


def verifier_nom_boutique_disponible(nom, boutique_pk=None):
    """Refuse un nom vide de sens, déjà pris, ou trop proche d'un nom pris.

    Partagé par Boutique.clean() (django-admin, full_clean) et le serializer
    (API), avec des messages distincts pour que le vendeur sache s'il doit
    simplement changer de nom ou s'en éloigner davantage. La contrainte
    unique sur `nom_normalise` reste le filet de sécurité en cas de course.
    """
    nom_normalise = normaliser_nom_boutique(nom)
    if not nom_normalise:
        raise ValidationError(
            "Le nom de la boutique doit contenir au moins une lettre ou un chiffre.",
            code='nom_sans_caractere_significatif',
        )
    autres = Boutique.objects.exclude(pk=boutique_pk) if boutique_pk else Boutique.objects.all()
    # Au plus un conflit possible : nom_normalise est unique en base.
    nom_existant = autres.filter(nom_normalise=nom_normalise).values_list('nom', flat=True).first()
    if nom_existant is None:
        return nom_normalise

    # « Identique » : même nom à la casse et aux espaces (répétés ou en
    # bordure) près. Toute autre différence (accents, ponctuation) ne
    # disparaît qu'à la normalisation complète : « trop proche ».
    def cle_nom_identique(valeur):
        return ' '.join(valeur.split()).casefold()

    if cle_nom_identique(nom_existant) == cle_nom_identique(nom):
        raise ValidationError("Ce nom de boutique est déjà utilisé.", code='nom_identique')
    raise ValidationError(
        "Ce nom est trop proche de celui d'une boutique existante "
        "(il ne s'en distingue que par des accents, de la ponctuation ou l'espacement entre les mots). "
        "Choisissez un nom plus distinct.",
        code='nom_trop_proche',
    )


class DemandeVendeurManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(statut_kyc=StatutKYC.EN_ATTENTE)


class DemandeVendeur(Utilisateur):
    """Les comptes en attente de validation vendeur.

    Modèle proxy : aucune table créée, aucune donnée dupliquée. Il sert
    uniquement à offrir une file de traitement dédiée (API admin + django-admin)
    sans toucher au module utilisateurs.
    """

    objects = DemandeVendeurManager()

    class Meta:
        proxy = True
        verbose_name = "Demande vendeur"
        verbose_name_plural = "Demandes vendeur (en attente)"