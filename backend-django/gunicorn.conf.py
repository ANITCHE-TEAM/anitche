"""Configuration de Gunicorn pour la production (chargée avec ``-c``)."""
import os

bind = "0.0.0.0:8000"

# Nombre de workers : GUNICORN_WORKERS, 3 par défaut. À ajuster d'après des
# mesures de charge, pas d'après une estimation.
workers = int(os.environ.get("GUNICORN_WORKERS", "3"))
if workers < 1:
    raise ValueError("GUNICORN_WORKERS doit être un entier supérieur ou égal à 1.")

# Un worker qui ne répond pas en 30 s est tué et remplacé ; à l'arrêt, les
# requêtes en cours disposent de 30 s pour se terminer.
timeout = 30
graceful_timeout = 30

# Recyclage périodique des workers pour borner les fuites mémoire ; le jitter
# évite qu'ils redémarrent tous en même temps.
max_requests = 1000
max_requests_jitter = 100

# Fichiers de heartbeat en mémoire : un disque lent ne provoque pas de faux
# timeouts.
worker_tmp_dir = "/dev/shm"

# Journaux sur la sortie standard et d'erreur, lus par Docker. Le chemin est
# journalisé sans la chaîne de requête, qui peut contenir un jeton, ni le
# référent.
accesslog = "-"
errorlog = "-"
access_log_format = '%(h)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s %(L)s "%(a)s"'
