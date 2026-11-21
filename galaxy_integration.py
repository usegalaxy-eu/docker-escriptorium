"""
Galaxy Interactive Tool integration for docker-escriptorium.

Provides a user-facing "stop session" control and the Galaxy entry point:

  * GET / redirects to the startup-imported document (IMPORT_DOCUMENT_NAME),
    so the Galaxy iframe lands directly in the document the user is supposed
    to work on (until the import has created it, a self-reloading
    "preparing" page is served; GALAXY_ENTRYPOINT_DOCUMENT=False disables
    this and serves the normal homepage),
  * injects a floating "Stop session and export to Galaxy" button into every
    HTML page (only when the GALAXY_STOP_BUTTON environment variable is
    truthy — the default for this image, which is meant to run as a Galaxy IT),
  * serves POST /galaxy/stop/, which writes a shutdown request file that
    the export-watcher service picks up: it waits for running tasks,
    saves all pending exports into EXPORT_DIR and then shuts the whole
    container down gracefully — so Galaxy can harvest the results from
    the job working directory right away.

With GALAXY_STOP_BUTTON=False the middleware is never installed and nothing
changes for standalone usage.

The stop endpoint is protected by Django's CSRF middleware (the injected
button sends the csrftoken cookie) and requires a logged-in user.
"""

import os

from django.utils.crypto import constant_time_compare
from django.http import HttpResponse, HttpResponseRedirect

STOP_FLAG = "/tmp/export-watcher-shutdown"
BUTTON_ID = "galaxy-stop-button"

# Entry point behaviour: when enabled (default for this Galaxy-first image),
# GET / redirects to the startup-imported document (IMPORT_DOCUMENT_NAME) so
# the Galaxy iframe lands directly in the document the user is supposed to
# work on. Until the startup import has created it, a self-reloading
# "preparing" page is served.
ENTRYPOINT_DOCUMENT_ENABLED = os.getenv(
    "GALAXY_ENTRYPOINT_DOCUMENT", "True"
).lower() in ("true", "1", "yes")
ENTRYPOINT_DOCUMENT_NAME = os.getenv("IMPORT_DOCUMENT_NAME") or "Imported document"

LOADING_PAGE = """
<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="3">
<title>eScriptorium — preparing your document</title>
<style>
  body { font-family: sans-serif; background: #f5f5f5; display: flex;
         align-items: center; justify-content: center; height: 100vh;
         margin: 0; color: #333; }
  .card { background: #fff; padding: 32px; border-radius: 8px;
          max-width: 460px; box-shadow: 0 2px 12px rgba(0,0,0,.15);
          text-align: center; }
  .bar { height: 4px; border-radius: 2px; background: #e8e3da;
         overflow: hidden; margin-bottom: 20px; }
  .bar span { display: block; height: 100%; width: 40%;
              background: #a94442; border-radius: 2px;
              animation: slide 1.6s ease-in-out infinite; }
  @keyframes slide { 0% { margin-left: -40%; } 100% { margin-left: 100%; } }
  .hint { color: #777; font-size: .9em; }
</style></head>
<body><div class="card">
  <div class="bar"><span></span></div>
  <h2>Preparing your document&hellip;</h2>
  <p>Your Galaxy datasets are being imported into eScriptorium.</p>
  <p class="hint">This page reloads automatically every 3 seconds.</p>
</div></body></html>
"""

GALAXY_LOGO_SVG = (
    '<svg width="15" height="15" viewBox="0 0 24 24" aria-hidden="true" '
    'style="flex:none;">'
    '<path d="M12 4c5 0 8.6 3.4 8.6 7.9 0 3.2-2.4 5.6-5.4 5.6-2.4 0-4.2-1.7'
    '-4.2-3.9 0-1.8 1.3-3.1 3-3.1 1.3 0 2.3 1 2.3 2.2" fill="none" '
    'stroke="#ffe08a" stroke-width="2.2" stroke-linecap="round"/>'
    '<path d="M12 20c-5 0-8.6-3.4-8.6-7.9C3.4 8.9 5.8 6.5 8.8 6.5c2.4 0 '
    '4.2 1.7 4.2 3.9 0 1.8-1.3 3.1-3 3.1-1.3 0-2.3-1-2.3-2.2" fill="none" '
    'stroke="#f9a825" stroke-width="2.2" stroke-linecap="round"/>'
    '</svg>'
)

BUTTON_HTML = """
<div id="%(id)s" style="position:fixed;bottom:16px;right:16px;z-index:100000;">
  <button title="Export all documents to Galaxy and shut down this interactive session"
          style="display:flex;align-items:center;gap:8px;background:#a94442;color:#fff;
                 border:none;border-radius:6px;padding:10px 16px;font-size:14px;
                 cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.4);">
    %(logo)s<span>Stop session and export to Galaxy</span>
  </button>
</div>
<script>
(function () {
  var box = document.getElementById('%(id)s');
  var btn = box.querySelector('button');
  btn.addEventListener('click', function () {
    if (btn.disabled) { return; }
    btn.disabled = true;
    btn.querySelector('span').textContent = 'Stopping\\u2026';
    var m = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
    fetch('/galaxy/stop/', {
      method: 'POST',
      headers: {'X-CSRFToken': m ? decodeURIComponent(m[1]) : ''}
    }).then(function (r) {
      if (!r.ok) { throw new Error('HTTP ' + r.status); }
      var overlay = document.createElement('div');
      overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);' +
        'z-index:100001;display:flex;align-items:center;justify-content:center;';
      overlay.innerHTML = '<div style="background:#fff;padding:24px;border-radius:8px;' +
        'max-width:440px;font-family:sans-serif;font-size:14px;color:#333;">' +
        '<strong>Stopping session\\u2026</strong>' +
        '<p style="margin:8px 0 0;">Pending exports are being saved to Galaxy ' +
        '(running tasks are waited for). You can close this window once the ' +
        'session ends.</p></div>';
      document.body.appendChild(overlay);
    }).catch(function () {
      btn.disabled = false;
      btn.querySelector('span').textContent = 'Stop failed \\u2014 try again';
    });
  });
})();
</script>
""" % {"id": BUTTON_ID, "logo": GALAXY_LOGO_SVG}

STOPPING_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><title>Stopping session</title></head>
<body style="font-family:sans-serif;background:#f5f5f5;display:flex;
             align-items:center;justify-content:center;height:100vh;margin:0;">
  <div style="background:#fff;padding:32px;border-radius:8px;max-width:460px;
              box-shadow:0 2px 12px rgba(0,0,0,.15);">
    <h2 style="margin-top:0;">Stopping session&hellip;</h2>
    <p>Pending exports are being saved and the interactive session is being
       shut down. You can close this window.</p>
  </div>
</body></html>
"""


def stop_button_enabled():
    return os.getenv("GALAXY_STOP_BUTTON", "True").lower() in ("true", "1", "yes")


def entrypoint_document_enabled():
    return ENTRYPOINT_DOCUMENT_ENABLED


def find_entrypoint_document():
    """Return the pk of the startup-imported document, or None if it does not
    exist (yet)."""
    from django.apps import apps
    document = apps.get_model("core", "Document")
    doc = (
        document.objects.filter(name=ENTRYPOINT_DOCUMENT_NAME)
        .order_by("pk")
        .first()
    )
    return doc.pk if doc else None


class GalaxyIntegrationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/galaxy/stop/":
            return self._handle_stop(request)

        if entrypoint_document_enabled() and request.method == "GET" \
                and request.path == "/":
            return self._entrypoint_response(request)

        response = self.get_response(request)
        if stop_button_enabled():
            response = self._inject_button(response)
        return response

    def _entrypoint_response(self, request):
        """The Galaxy iframe loads /: land the user directly in the imported
        document. While the startup import has not created it yet, serve a
        self-reloading 'preparing' page."""
        if not request.user.is_authenticated:
            response = self.get_response(request)
            if stop_button_enabled():
                response = self._inject_button(response)
            return response
        # Short-circuiting the view means Django's CSRF middleware never ran
        # get_token for this response — call it so the csrftoken cookie is
        # still set (the stop button needs it).
        from django.middleware.csrf import get_token
        get_token(request)
        doc_pk = find_entrypoint_document()
        if doc_pk:
            # 302 on purpose: never let browsers cache the redirect — the
            # document set may change during the session.
            return HttpResponseRedirect(f"/document/{doc_pk}/")
        # Document not imported yet: self-reloading "preparing" page, with
        # the stop button so the session can be ended during the import.
        response = HttpResponse(LOADING_PAGE, content_type="text/html")
        if stop_button_enabled():
            response = self._inject_button(response)
        return response

    def _handle_stop(self, request):
        if not stop_button_enabled():
            return HttpResponse("Not found.", status=404)
        if request.method != "POST":
            return HttpResponse("Use POST to stop the session.", status=405)
        if not request.user.is_authenticated:
            return HttpResponse("Authentication required.", status=403)

        # The request is handled inside middleware, before Django's CSRF
        # view protection runs — do the (same-origin) check manually.
        token = request.headers.get("X-CSRFToken") or request.headers.get("X-CSRF-Token")
        cookie = request.COOKIES.get("csrftoken")
        if not token or not cookie or not constant_time_compare(token, cookie):
            return HttpResponse("CSRF check failed.", status=403)

        # The export-watcher polls for this file: it flushes pending
        # exports and then shuts the container down (see export-watcher.py).
        with open(STOP_FLAG, "w") as fh:
            fh.write("stop")
        return HttpResponse(STOPPING_PAGE, content_type="text/html")

    def _inject_button(self, response):
        content_type = response.get("Content-Type", "")
        if not content_type.startswith("text/html"):
            return response
        if getattr(response, "streaming", False):
            return response
        try:
            content = response.content.decode(response.charset)
        except (AttributeError, UnicodeDecodeError):
            return response
        if BUTTON_ID in content:
            return response
        marker = content.lower().rfind("</body>")
        if marker < 0:
            return response
        content = content[:marker] + BUTTON_HTML + content[marker:]
        response.content = content.encode(response.charset)
        response["Content-Length"] = str(len(response.content))
        return response
