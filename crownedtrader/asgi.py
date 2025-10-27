"""
ASGI config for crownedtrader project.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'crownedtrader.settings')

application = get_asgi_application()

