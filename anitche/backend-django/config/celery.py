import os
import sys
from celery import Celery

_default_settings = 'config.settings.test' if 'test' in sys.argv else 'config.settings.dev'
os.environ.setdefault('DJANGO_SETTINGS_MODULE', _default_settings)

app = Celery('anitche')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
app.conf.beat_scheduler = 'django_celery_beat.schedulers:DatabaseScheduler'