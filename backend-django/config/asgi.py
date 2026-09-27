import os

from django.core.asgi import get_asgi_application

# A05:2025 (Security Misconfiguration) : ce fichier est le point
# d'entree reel d'un serveur de production (gunicorn/uvicorn), pas
# un outil de dev local (contrairement a manage.py, qui lui doit
# rester sur dev par defaut pour l'ergonomie locale). Si la variable
# DJANGO_SETTINGS_MODULE n'est jamais definie explicitement, le repli
# doit etre le choix le plus SUR (prod, qui echoue bruyamment si
# SECRET_KEY/ALLOWED_HOSTS/CORS_ALLOWED_ORIGINS manquent), jamais
# 'dev' (DEBUG=True, CORS ouvert a tous, cle secrete par defaut).
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.prod')

application = get_asgi_application()