"""
Django settings for crownedtrader project.
"""

from pathlib import Path
import os
import logging
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/4.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-your-secret-key-change-in-production')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get('DEBUG', 'True') == 'True'

# Allow any host in debug mode for development, restrict in production
if DEBUG:
    # ALLOWED_HOSTS = ['*']  # Allow any host in debug mode
    # CSRF_TRUSTED_ORIGINS = ['http://*', 'https://*']  # Allow any origin in debug mode
    ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '132.148.77.219', 'crowntraders.stockswithjosh.com']
    CSRF_TRUSTED_ORIGINS = ['http://127.0.0.1:8000', 'http://localhost:8000', 'http://132.148.77.219:8000', 'https://crowntraders.stockswithjosh.com']
else:
    ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '132.148.77.219', 'crowntraders.stockswithjosh.com']
    CSRF_TRUSTED_ORIGINS = ['http://127.0.0.1:8000', 'http://localhost:8000', 'http://132.148.77.219:8000', 'https://crowntraders.stockswithjosh.com']


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'signals',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'signals.middleware.AgreementRequiredMiddleware',
]

ROOT_URLCONF = 'crownedtrader.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'crownedtrader.wsgi.application'


# Database
# https://docs.djangoproject.com/en/4.2/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


# Password validation
# https://docs.djangoproject.com/en/4.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/4.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.2/howto/static-files/

STATIC_URL = 'static/'

# Discord Configuration - Load from environment variables
# Option 1: Webhook URL (recommended - simpler, no permissions needed)
DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL', '')

# Option 2: Bot Token (requires bot permissions)
DISCORD_BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN', '')
DISCORD_CHANNEL_ID = os.environ.get('DISCORD_CHANNEL_ID', '')

# Market data (Polygon)
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY', '')
# Cache quote responses for this many seconds to reduce API call count (rate limits)
POLYGON_QUOTE_CACHE_SECONDS = int(os.environ.get('POLYGON_QUOTE_CACHE_SECONDS', '30'))

# Automatic position tracking: background thread checks open auto positions every N seconds
# (0 = disabled; rely on cron + manage.py check_auto_positions instead)
AUTO_TRACKING_BACKGROUND_INTERVAL_SECONDS = int(os.environ.get('AUTO_TRACKING_BACKGROUND_INTERVAL_SECONDS', '60'))

# Additional settings
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Default primary key field type
# https://docs.djangoproject.com/en/4.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Login URL for authentication
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'

# CSRF and Session settings for HTTPS
# If behind a reverse proxy (Nginx) with SSL termination, Django needs to trust proxy headers
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

CSRF_COOKIE_SECURE = True  # Set to True if using HTTPS
SESSION_COOKIE_SECURE = True  # Set to True if using HTTPS
CSRF_COOKIE_HTTPONLY = False
CSRF_USE_SESSIONS = False
CSRF_COOKIE_SAMESITE = 'Lax'

