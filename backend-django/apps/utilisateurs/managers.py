from django.contrib.auth.models import BaseUserManager
from django.db.models.functions import Lower


class UtilisateurManager(BaseUserManager):
    """
    Manager personnalisé du modèle Utilisateur.

    Il centralise la logique de création des utilisateurs
    standards et des superutilisateurs.
    """

    @classmethod
    def normalize_email(cls, email):
        """
        Email tel qu'il est enregistré : entièrement en minuscules.

        Deux adresses qui ne diffèrent que par la casse désignent le même
        compte ; toute écriture de l'email passe par cette méthode.
        """
        return super().normalize_email(email).lower()

    def avec_email(self, email):
        """
        Comptes (un au plus) dont l'email est celui-ci, quelle que soit
        la casse saisie.

        Égalité exacte sur LOWER(email) : la recherche s'appuie sur
        l'index de la contrainte d'unicité insensible à la casse.
        """
        return self.alias(email_minuscules=Lower('email')).filter(
            email_minuscules=self.normalize_email(email)
        )

    def get_by_natural_key(self, email):
        """Compte correspondant à l'email saisi à la connexion."""
        return self.avec_email(email).get()

    def create_user(self, email, password=None, **extra_fields):
        """
        Crée un utilisateur classique.

        Le mot de passe est automatiquement haché grâce
        à set_password() avant l'enregistrement.
        """

        # L'email est obligatoire car il sert d'identifiant.
        if not email:
            raise ValueError("L'email est obligatoire")

        # Uniformise le format de l'adresse email.
        email = self.normalize_email(email)

        # Création de l'instance utilisateur.
        user = self.model(email=email, **extra_fields)

        # Hash sécurisé du mot de passe.
        user.set_password(password)

        # Sauvegarde en base de données.
        user.save(using=self._db)

        return user

    def create_superuser(self, email, password=None, **extra_fields):
        """
        Crée un superutilisateur.

        Les permissions administrateur sont imposées
        afin de garantir les droits complets.
        """

        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        # Import local (et non en haut du fichier) : models.py importe déjà
        # UtilisateurManager depuis ce module, donc un import de niveau
        # module de `.models` ici créerait une boucle d'import (models.py
        # → managers.py → models.py) qui empêche Django de démarrer.
        from .models import Role
        extra_fields.setdefault('role', Role.SUPER_ADMIN)

        # Vérification de cohérence des permissions.
        if extra_fields.get('is_staff') is not True:
            raise ValueError(
                "Le superutilisateur doit avoir is_staff=True"
            )

        if extra_fields.get('is_superuser') is not True:
            raise ValueError(
                "Le superutilisateur doit avoir is_superuser=True"
            )

        return self.create_user(email, password, **extra_fields)