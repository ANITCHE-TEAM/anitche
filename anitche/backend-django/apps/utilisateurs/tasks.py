from celery import shared_task
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings

from .models import CodeOTP


@shared_task
def nettoyer_otp_expires():
    """
    Tâche asynchrone exécutée par Celery.

    Supprime les codes OTP expirés depuis plus de 24 heures
    afin de limiter la taille de la base de données.
    """

    # Seuil à partir duquel les OTP sont considérés
    # comme suffisamment anciens pour être supprimés.
    seuil = timezone.now() - timezone.timedelta(hours=24)

    # Suppression des OTP expirés avant ce seuil.
    supprimes, _ = CodeOTP.objects.filter(
        date_expiration__lt=seuil
    ).delete()

    # Retourne un résumé utile pour les logs ou le monitoring.
    return f"{supprimes} codes OTP supprimés."




@shared_task
def envoyer_code_otp_email(email_destinataire, code, type_usage):
    """Envoie un code OTP par email, en tâche asynchrone (ne bloque pas la requête HTTP)."""
    sujets = {
        'inscription': "Confirmez votre inscription",
        'mdp_oublie': "Réinitialisation de votre mot de passe",
        'changement_email': "Confirmez votre nouvelle adresse email",
        'changement_telephone': "Confirmez votre nouveau numéro de téléphone",
    }
    sujet = sujets.get(type_usage, "Votre code de vérification ANITCHE")

    message = (
        f"Votre code de vérification est : {code}\n\n"
        f"Ce code expire dans {settings.__dict__.get('OTP_DUREE_VALIDITE_MINUTES', 10)} minutes.\n"
        "Si vous n'êtes pas à l'origine de cette demande, ignorez ce message."
    )

    send_mail(
        subject=sujet,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email_destinataire],
        fail_silently=False,
    )