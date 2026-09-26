from celery import shared_task
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings

from .models import CodeOTP


@shared_task
def purger_tokens_expires():
    """
    Tâche asynchrone exécutée par Celery.

    Supprime les OutstandingToken (refresh tokens émis) expirés. Sans
    cela, cette table grossit indéfiniment à chaque connexion : elle
    n'est jamais purgée automatiquement par simplejwt. La suppression
    d'un OutstandingToken entraîne, par CASCADE, la suppression du
    BlacklistedToken associé s'il existe.

    Planifiée quotidiennement via CELERY_BEAT_SCHEDULE (config/settings/
    base.py) — équivalent de la commande `manage.py flushexpiredtokens`
    fournie par rest_framework_simplejwt.token_blacklist.
    """
    from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

    supprimes, _ = OutstandingToken.objects.filter(
        expires_at__lt=timezone.now()
    ).delete()

    return f"{supprimes} tokens expirés supprimés."


@shared_task
def nettoyer_otp_expires():
    """
    Tâche asynchrone exécutée par Celery.

    Supprime les codes OTP expirés depuis plus de 24 heures
    afin de limiter la taille de la base de données.

    Planifiée quotidiennement via CELERY_BEAT_SCHEDULE (config/settings/
    base.py).
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
        f"Ce code expire dans {CodeOTP.DUREE_VALIDITE_MINUTES} minutes.\n"
        "Si vous n'êtes pas à l'origine de cette demande, ignorez ce message."
    )

    send_mail(
        subject=sujet,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email_destinataire],
        fail_silently=False,
    )


@shared_task
def envoyer_notification_connexion(email_destinataire, adresse_ip, user_agent):
    """F-04 (audit sécurité) : notifie le titulaire du compte à chaque
    connexion réussie (classique ou Google), avec l'IP et le user-agent,
    pour qu'il puisse repérer une connexion qu'il n'a pas lui-même
    initiée. Envoyée uniquement en cas de succès, jamais sur un échec
    d'authentification (voir LoginThrottleView.post / ConnexionGoogleView)."""
    sujet = "Nouvelle connexion à votre compte ANITCHE"
    message = (
        "Une connexion vient d'avoir lieu sur votre compte ANITCHE.\n\n"
        f"Adresse IP : {adresse_ip}\n"
        f"Appareil / navigateur : {user_agent}\n\n"
        "Si ce n'est pas vous, changez votre mot de passe immédiatement "
        "et contactez le support."
    )

    send_mail(
        subject=sujet,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email_destinataire],
        fail_silently=False,
    )
