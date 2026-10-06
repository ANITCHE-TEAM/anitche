from django.contrib.auth.models import BaseUserManager


class UtilisateurManager(BaseUserManager):
    """
    Manager personnalisé du modèle Utilisateur.

    Il centralise la logique de création des utilisateurs
    standards et des superutilisateurs.
    """

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