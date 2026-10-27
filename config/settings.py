import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.environ.get("DEBUG", "0") == "1"
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "development-only-not-for-production"
    else:
        raise ImproperlyConfigured("Set a unique SECRET_KEY (at least 50 characters) or DEBUG=1 for local development.")
if not DEBUG and len(SECRET_KEY) < 50:
    raise ImproperlyConfigured("Production SECRET_KEY must contain at least 50 characters.")
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",") if h.strip()]
INSTALLED_APPS = ["django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles", "lab"]
MIDDLEWARE = ["django.middleware.security.SecurityMiddleware", "lab.middleware.ServiceMiddleware", "django.contrib.sessions.middleware.SessionMiddleware", "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware", "django.contrib.auth.middleware.AuthenticationMiddleware", "lab.login_limits.LoginLimitMiddleware", "django.contrib.messages.middleware.MessageMiddleware", "django.middleware.clickjacking.XFrameOptionsMiddleware"]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [BASE_DIR / "templates"], "APP_DIRS": True, "OPTIONS": {"context_processors": ["django.template.context_processors.request", "django.contrib.auth.context_processors.auth", "django.contrib.messages.context_processors.messages"]}}]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": os.environ.get("DATABASE_PATH", BASE_DIR / "db.sqlite3"), "OPTIONS": {"timeout": 30, "transaction_mode": "IMMEDIATE"}}}
if os.environ.get("TEST_DATABASE_PATH"):
    DATABASES["default"]["TEST"] = {"NAME": os.environ["TEST_DATABASE_PATH"]}
AUTH_PASSWORD_VALIDATORS = [{"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"}, {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"}, {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"}, {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"}]
LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = Path(os.environ.get("STATIC_ROOT", BASE_DIR / "staticfiles"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"
DATA_UPLOAD_MAX_MEMORY_SIZE = 16777216
SESSION_COOKIE_HTTPONLY = True
HTTPS = os.environ.get("HTTPS", "0" if DEBUG else "1") == "1"
SESSION_COOKIE_SECURE = HTTPS
CSRF_COOKIE_SECURE = HTTPS
CSRF_TRUSTED_ORIGINS = [s.strip() for s in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if s.strip()]
# Web is reachable only through the trusted Compose reverse proxy in production.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = HTTPS
SECURE_HSTS_SECONDS = 31536000 if HTTPS else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = HTTPS
SECURE_HSTS_PRELOAD = HTTPS
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
JUDGE_IMAGE = os.environ.get("JUDGE_IMAGE", "contest-judge:local")
JUDGE_PREFIX = os.environ.get("JUDGE_PREFIX", "department-judge")
WORKER_LOCK = os.environ.get("WORKER_LOCK", str(BASE_DIR / ".worker.lock"))
LOGIN_ACCOUNT_LIMIT = int(os.environ.get("LOGIN_ACCOUNT_LIMIT", "8"))
LOGIN_IP_LIMIT = int(os.environ.get("LOGIN_IP_LIMIT", "300"))
LOGIN_WINDOW_SECONDS = int(os.environ.get("LOGIN_WINDOW_SECONDS", "900"))
LOGIN_TRUST_PROXY = os.environ.get("LOGIN_TRUST_PROXY", "0") == "1"
QUEUE_WARNING_SECONDS = int(os.environ.get("QUEUE_WARNING_SECONDS", "120"))
LOGGING = {
    "version": 1, "disable_existing_loggers": False,
    "formatters": {"judge": {"format": "{asctime} {levelname} {name} {message}", "style": "{"}},
    "handlers": {"judge_console": {"class": "logging.StreamHandler", "formatter": "judge"}},
    "loggers": {name: {"handlers": ["judge_console"], "level": "INFO", "propagate": False}
                for name in ("judge", "lab.management.commands.judge_worker")},
}
