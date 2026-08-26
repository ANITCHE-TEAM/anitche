from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.text import slugify

from apps.utilisateurs.models import Role, StatutKYC, Utilisateur


class BoutiqueQuerySet(models.QuerySet):
    def ouvertes(self):
        return self.filter(est_active=True)

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
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    description = models.TextField(blank=True)

    logo = models.ImageField(upload_to='boutiques/logos/', null=True, blank=True)
    banniere = models.ImageField(upload_to='boutiques/bannieres/', null=True, blank=True)

    telephone_contact = models.CharField(max_length=20, blank=True)
    email_contact = models.EmailField(blank=True)
    adresse = models.TextField(blank=True)
    ville = models.CharField(max_length=100, blank=True)

    est_active = models.BooleanField(
        default=True,
        help_text="Décochez pour fermer temporairement la boutique (elle disparaît du catalogue public).",
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
        un vendeur non validé ou une boutique fermée ne publie rien.
        """
        return self.est_active and self.proprietaire.is_active and self.vendeur_est_valide

    def clean(self):
        # Contrôle uniquement à la création : une boutique déjà existante doit
        # rester modifiable même si le vendeur est suspendu par la suite.
        if self._state.adding and not self.vendeur_est_valide:
            raise ValidationError(
                "Seul un compte vendeur validé (statut_kyc = validé) peut ouvrir une boutique."
            )

    def _generer_slug_unique(self):
        base = slugify(self.nom)[:120] or 'boutique'
        slug = base
        compteur = 2
        while Boutique.objects.filter(slug=slug).exclude(pk=self.pk).exists():
            slug = f"{base}-{compteur}"
            compteur += 1
        return slug

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._generer_slug_unique()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.nom} ({self.proprietaire.email})"

    class Meta:
        verbose_name = "Boutique"
        verbose_name_plural = "Boutiques"
        ordering = ['nom']


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
