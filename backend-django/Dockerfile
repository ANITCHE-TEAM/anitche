FROM python:3.12-slim

# Empêche Python de bufferiser stdout/stderr (logs visibles en direct dans Docker)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dépendances système nécessaires à psycopg2
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Utilisateur sans privilèges pour Gunicorn, le worker et beat : le code
# appartient à root, en lecture seule pour l'application. Seuls MEDIA_ROOT et
# STATIC_ROOT lui appartiennent ; un volume nommé neuf monté sur ces dossiers
# reprend ce propriétaire au premier démarrage.
RUN useradd --system --no-create-home app

COPY . .

RUN mkdir -p /app/media /app/staticfiles \
    && chown app:app /app/media /app/staticfiles

USER app

EXPOSE 8000

# En dev, docker-compose écrase cette commande par "runserver".
# En prod, docker-compose.prod.yml utilise celle-ci (gunicorn + réglages prod).
CMD ["gunicorn", "config.wsgi:application", "-c", "gunicorn.conf.py"]
