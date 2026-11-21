"""
Optional authentication bypass for docker-escriptorium.

When the DISABLE_AUTH environment variable is truthy ("true", "1", "yes"),
the AutoLoginMiddleware logs every anonymous request in as the first
superuser (the bootstrap account), so visitors can browse the site and use
the API without creating an account or logging in.

This is the DEFAULT for this image: it is meant to run as a Galaxy
Interactive Tool, where the container is single-user and reachable only
through Galaxy's proxied entry point. Set DISABLE_AUTH=False to get normal
login behaviour (e.g. for a multi-user standalone deployment).

A real Django session is established (not just request.user being swapped),
so websockets and DRF's SessionAuthentication behave exactly like a normal
login.

WARNING: when enabled this grants full admin access to anyone who can reach
the site. Only run it on trusted/private networks.
"""

import os

from django.contrib.auth import get_user_model, login


def auth_disabled():
    return os.getenv('DISABLE_AUTH', 'True').lower() in ('true', '1', 'yes')


class AutoLoginMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Never auto-login API requests: DRF's SessionAuthentication would
        # then treat non-browser clients (scripts, curl) as authenticated
        # sessions and enforce CSRF on their POSTs, breaking token-based
        # API usage. Browser users get their session from the page loads;
        # unauthenticated API clients keep using token auth.
        if request.path.startswith('/api/'):
            return self.get_response(request)

        if request.user.is_anonymous:
            user_model = get_user_model()
            user = (
                user_model.objects.filter(is_superuser=True, is_active=True)
                .order_by('pk')
                .first()
            )
            if user is not None:
                request.user = user
                login(
                    request,
                    user,
                    backend='django.contrib.auth.backends.ModelBackend',
                )
        return self.get_response(request)
