import base64
import logging
import os
from typing import Optional

from cachelib.redis import RedisCache
from celery.schedules import crontab
from flask_appbuilder.security.manager import AUTH_OAUTH

logger = logging.getLogger()


def get_env_variable(var_name: str, default: Optional[str] = None) -> str:
    try:
        return os.environ[var_name]
    except KeyError:
        if default is not None:
            return default
        raise OSError(f"The environment variable {var_name} was missing, abort...")


# ── Database ──────────────────────────────────────────────────────────────────
SQLALCHEMY_DATABASE_URI = get_env_variable("SQLALCHEMY_DATABASE_URI")
SQLALCHEMY_ECHO = False
SQLALCHEMY_POOL_SIZE = 45
SQLALCHEMY_MAX_OVERFLOW = 30
SQLALCHEMY_POOL_TIMEOUT = 1800

# ── Row / query limits ────────────────────────────────────────────────────────
ROW_LIMIT = 50000
VIZ_ROW_LIMIT = 10000
SAMPLES_ROW_LIMIT = 1000
FILTER_SELECT_ROW_LIMIT = 10000
QUERY_SEARCH_LIMIT = 1000
SQL_MAX_ROW = 100000
DISPLAY_MAX_ROW = 1000

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_HOST = get_env_variable("REDIS_HOST")
REDIS_PORT = get_env_variable("REDIS_PORT", "6379")
REDIS_CELERY_DB = get_env_variable("REDIS_CELERY_DB", "0")
REDIS_RESULTS_DB = get_env_variable("REDIS_RESULTS_DB", "1")

# ── Cache ─────────────────────────────────────────────────────────────────────
RESULTS_BACKEND = RedisCache(
    host=REDIS_HOST,
    port=int(REDIS_PORT),
    db=int(REDIS_RESULTS_DB),
    key_prefix="superset_results_",
    default_timeout=1800,
)

CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 3600,
    "CACHE_KEY_PREFIX": "superset_",
    "CACHE_REDIS_HOST": REDIS_HOST,
    "CACHE_REDIS_PORT": REDIS_PORT,
    "CACHE_REDIS_DB": REDIS_RESULTS_DB,
}

FILTER_STATE_CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 86400,
    "CACHE_KEY_PREFIX": "superset_filter_cache",
    "CACHE_REDIS_HOST": REDIS_HOST,
    "CACHE_REDIS_PORT": REDIS_PORT,
    "CACHE_REDIS_DB": REDIS_RESULTS_DB,
}

EXPLORE_FORM_DATA_CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 86400,
    "CACHE_KEY_PREFIX": "superset_form_data_cache",
    "CACHE_REDIS_HOST": REDIS_HOST,
    "CACHE_REDIS_PORT": REDIS_PORT,
    "CACHE_REDIS_DB": REDIS_RESULTS_DB,
}

DATA_CACHE_CONFIG = CACHE_CONFIG

# Prevents cache stampede when many users hit a cold dashboard simultaneously.
DISTRIBUTED_COORDINATION_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_KEY_PREFIX": "superset_lock_",
    "CACHE_REDIS_HOST": REDIS_HOST,
    "CACHE_REDIS_PORT": REDIS_PORT,
    "CACHE_REDIS_DB": REDIS_RESULTS_DB,
}
DISTRIBUTED_LOCK_DEFAULT_TTL = 120

# ── Celery ────────────────────────────────────────────────────────────────────
class CeleryConfig:
    broker_url = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_CELERY_DB}"
    imports = ("superset.sql_lab",)
    result_backend = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_RESULTS_DB}"
    worker_prefetch_multiplier = 1
    task_acks_late = False
    beat_schedule = {
        "reports.scheduler": {
            "task": "reports.scheduler",
            "schedule": crontab(minute="*", hour="*"),
        },
        "reports.prune_log": {
            "task": "reports.prune_log",
            "schedule": crontab(minute=10, hour=0),
        },
    }

CELERY_CONFIG = CeleryConfig

# ── Timeouts ──────────────────────────────────────────────────────────────────
SUPERSET_WEBSERVER_TIMEOUT = 1800
SUPERSET_TIMEOUT = 1800
GUNICORN_TIMEOUT = 1800
GUNICORN_KEEPALIVE = 1800
SQLLAB_ASYNC_TIME_LIMIT_SEC = 1800
SQLLAB_TIMEOUT = 1800
SQLLAB_CTAS_NO_LIMIT = True

# ── Features ──────────────────────────────────────────────────────────────────
FEATURE_FLAGS = {
    "ALERT_REPORTS": True,
    "ENABLE_TEMPLATE_PROCESSING": True,
    "DRILL_TO_DETAIL": True,
    "EMBEDDED_SUPERSET": True,
    "DASHBOARD_RBAC": True,
    "DRILL_BY": True,
    "TAGGING_SYSTEM": True,
    "HORIZONTAL_FILTER_BAR": True,
}
ALERT_REPORTS_NOTIFICATION_DRY_RUN = True
WEBDRIVER_BASEURL = "http://superset:8088/"
WEBDRIVER_BASEURL_USER_FRIENDLY = WEBDRIVER_BASEURL

# ── Security ──────────────────────────────────────────────────────────────────
ENABLE_PROXY_FIX = True
WTF_CSRF_ENABLED = False
OVERRIDE_HTTP_HEADERS = {"X-Frame-Options": "ALLOWALL"}
TALISMAN_ENABLED = False
GUEST_ROLE_NAME = "All_Dashboard_Reader"
SUPERSET_DASHBOARD_POSITION_DATA_LIMIT = 131070

# ── OAuth / SSO (optional) ────────────────────────────────────────────────────
# Set client_id, client_secret, and ol_base_url in Secrets Manager to enable SSO.
if client_id := get_env_variable("client_id", ""):
    from custom_sso_security_manager import CustomSsoSecurityManager

    client_secret = get_env_variable("client_secret")
    ol_base_url = get_env_variable("ol_base_url")
    authorization = base64.b64encode(f"{client_id}:{client_secret}".encode())

    CUSTOM_SECURITY_MANAGER = CustomSsoSecurityManager
    AUTH_TYPE = AUTH_OAUTH
    OAUTH_PROVIDERS = [
        {
            "name": "onelogin",
            "token_key": "access_token",
            "icon": "fa-address-card",
            "remote_app": {
                "client_id": client_id,
                "client_secret": client_secret,
                "client_kwargs": {
                    "scope": "openid params groups",
                    "response_type": "code",
                },
                "jwks_uri": f"https://{ol_base_url}/oidc/2/certs",
                "access_token_method": "POST",
                "access_token_params": {"client_id": client_id},
                "access_token_headers": {
                    "Authorization": f"Basic {authorization.decode()}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                "api_base_url": f"https://{ol_base_url}/",
                "access_token_url": f"https://{ol_base_url}/oidc/2/token",
                "authorize_url": f"https://{ol_base_url}/oidc/2/auth",
            },
        }
    ]
    AUTH_USER_REGISTRATION = True
    AUTH_USER_REGISTRATION_ROLE = "Public"
    AUTH_ROLES_SYNC_AT_LOGIN = True
    AUTH_ROLES_MAPPING = {"Admin": ["Admin"]}
    PERMANENT_SESSION_LIFETIME = 86400

# ── Optional local overrides ──────────────────────────────────────────────────
try:
    import superset_config_docker
    from superset_config_docker import *  # noqa

    logger.info(f"Loaded Docker config overrides at [{superset_config_docker.__file__}]")
except ImportError:
    logger.info("Using default Docker config...")
