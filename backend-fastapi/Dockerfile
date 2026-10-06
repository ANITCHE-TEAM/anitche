FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Aucun paquet système : toutes les dépendances (asyncpg compris) ont des
# wheels pour Python 3.12. Image plus petite, moins de surface d'attaque.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Utilisateur sans privilèges : le code appartient à root, en lecture seule
# pour l'application.
RUN useradd --system --no-create-home app

COPY . .

USER app

EXPOSE 8001

# Pas de --reload ici (ajouté uniquement par le compose de dev).
# --no-proxy-headers : l'IP du client est lue par app/core/network.py
# (TRUSTED_PROXY_COUNT), une seule règle, testée.
# --no-access-log : journal d'accès sans chaîne de requête (app/core/access_log.py).
# --ws-max-size 8192 : barrière extérieure (l'app refuse au-delà de 4 096
# octets, code 1009) ; ping WebSocket explicite toutes les 20 s.
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8001", "--no-proxy-headers", "--no-access-log", "--ws-max-size", "8192", "--ws-ping-interval", "20", "--ws-ping-timeout", "20"]
