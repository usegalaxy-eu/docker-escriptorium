# --- docker-escriptorium: optional auth bypass -----------------------------
# Appended to app/escriptorium/settings.py by the docker-escriptorium build.
# See escriptorium/autologin.py, escriptorium/galaxy_integration.py and the
# DISABLE_AUTH / GALAXY_STOP_BUTTON documentation in README.
from escriptorium import autologin  # noqa: E402
from escriptorium import galaxy_integration  # noqa: E402

_auth_mw = 'django.contrib.auth.middleware.AuthenticationMiddleware'

# Galaxy "stop session" button / endpoints. Inserted before the auto-login
# middleware so that the auto-login runs first on the request path and the
# stop endpoint sees the logged-in user.
MIDDLEWARE.insert(
    MIDDLEWARE.index(_auth_mw) + 1,
    'escriptorium.galaxy_integration.GalaxyIntegrationMiddleware',
)

if autologin.auth_disabled():
    MIDDLEWARE.insert(
        MIDDLEWARE.index(_auth_mw) + 1,
        'escriptorium.autologin.AutoLoginMiddleware',
    )
# ---------------------------------------------------------------------------
