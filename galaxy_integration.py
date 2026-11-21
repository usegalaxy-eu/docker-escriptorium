"""Readiness, normal Django CSRF protection, and a verified save/stop control."""

import html
import os
import secrets

from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_protect

from galaxy_runtime import (
    INTERNAL_TOKEN,
    SHUTDOWN,
    STARTUP,
    STOP_REQUEST,
    atomic_json,
    read_json,
    status,
    write_lock,
)

ACTIVE_PHASES = {
    "requested",
    "draining",
    "exporting",
    "models",
    "snapshotting",
    "complete",
    "failed",
}
BUTTON_ID = "galaxy-stop-button"


def stop_button_enabled():
    return os.getenv("GALAXY_STOP_BUTTON", "True").lower() in ("true", "1", "yes")


def frame_ancestors():
    """CSP frame-ancestors value for Galaxy iframe embedding, or None when
    embedding should stay blocked (Django's default). The Galaxy IT passes
    $__galaxy_url__ (trailing slash stripped here), so this works on any
    Galaxy server without configuration."""
    value = os.getenv("GALAXY_FRAME_ANCESTORS", "*").strip().rstrip("/")
    if value.lower() in ("false", "0", "no", "off", ""):
        return None
    return value


def internal_request(request):
    supplied = request.headers.get("X-Galaxy-Internal", "")
    return bool(
        supplied
        and INTERNAL_TOKEN.exists()
        and secrets.compare_digest(supplied, INTERNAL_TOKEN.read_text().strip())
    )


@csrf_protect
def control(request):
    if not stop_button_enabled():
        return HttpResponse("Not found", status=404)
    if not request.user.is_authenticated:
        return HttpResponse("Authentication required", status=403)
    if request.path == "/galaxy/status/" and request.method == "GET":
        return JsonResponse(
            {"startup": read_json(STARTUP), "shutdown": read_json(SHUTDOWN)}
        )
    if request.method != "POST":
        return HttpResponse("Use POST", status=405)
    # Taking the exclusive lock drains browser writes already in progress.
    # Later browser writes see the requested state and cannot start new work.
    with write_lock(exclusive=True):
        phase = read_json(SHUTDOWN).get("phase")
        if request.path == "/galaxy/resume/":
            if phase != "failed":
                return HttpResponse("Only a failed save can be resumed", status=409)
            STOP_REQUEST.unlink(missing_ok=True)
            status(SHUTDOWN, "running", "Session is running")
        elif request.path == "/galaxy/stop/":
            if phase not in ACTIVE_PHASES or phase == "failed":
                status(SHUTDOWN, "requested", "Finishing work and saving your session")
                atomic_json(STOP_REQUEST, {"requested": True})
        else:
            return HttpResponse("Not found", status=404)
    return JsonResponse(read_json(SHUTDOWN))


BUTTON_HTML = r"""
<div id="galaxy-stop-button" style="position:fixed;bottom:16px;right:16px;z-index:100000">
<button style="background:#a94442;color:white;border:0;border-radius:6px;padding:12px;cursor:pointer;display:flex;align-items:center;gap:8px"><img src="/static/images/galaxy-logo.png" alt="" aria-hidden="true" width="28" height="22" style="object-fit:contain;background:white;border-radius:3px;padding:2px">Save results and stop session</button>
</div>
<script>
(function () {
  const box = document.getElementById('galaxy-stop-button');
  const button = box.querySelector('button');
  let overlay, message, actions;
  function csrf() {
    const token = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
    return token ? decodeURIComponent(token[1]) : '';
  }
  async function post(path) {
    const response = await fetch(path, {method: 'POST', headers: {'X-CSRFToken': csrf()}});
    if (!response.ok) throw new Error('Request failed (HTTP ' + response.status + '). Save your edits and try again.');
    return response.json();
  }
  function show() {
    if (overlay) return;
    overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100001;display:flex;align-items:center;justify-content:center';
    overlay.innerHTML = '<div style="background:white;padding:28px;border-radius:8px;max-width:520px;color:#333;font-family:sans-serif"><h2>Saving your session</h2><p></p><div></div></div>';
    message = overlay.querySelector('p');
    actions = overlay.querySelector('div').lastElementChild;
    document.body.appendChild(overlay);
  }
  function failure(text) {
    message.textContent = text;
    actions.replaceChildren();
    for (const [label, callback] of [['Retry saving', stop], ['Resume editing', async function () {
      try { await post('/galaxy/resume/'); overlay.remove(); overlay = null; button.disabled = false; }
      catch (error) { message.textContent = error.message; }
    }]]) {
      const action = document.createElement('button');
      action.textContent = label;
      action.style.marginRight = '12px';
      action.onclick = callback;
      actions.appendChild(action);
    }
  }
  async function poll() {
    try {
      const response = await fetch('/galaxy/status/');
      if (!response.ok) throw new Error('Cannot read save status');
      const data = await response.json();
      message.textContent = data.shutdown.message || 'Saving results…';
      if (data.shutdown.phase === 'failed') { failure(data.shutdown.message); return; }
      if (data.shutdown.phase === 'complete') {
        message.textContent = 'Results and the resumable session have been saved. You can close this window.';
        return;
      }
      setTimeout(poll, 1000);
    } catch (error) {
      message.textContent = 'Connection closed. Check the job status in Galaxy before leaving.';
    }
  }
  async function stop() {
    button.disabled = true;
    show();
    actions.replaceChildren();
    message.textContent = 'Finishing running tasks before exporting…';
    try { await post('/galaxy/stop/'); poll(); }
    catch (error) { failure(error.message); }
  }
  button.onclick = function () { if (document.activeElement) document.activeElement.blur(); setTimeout(stop, 750); };
})();
</script>
"""


class GalaxyIntegrationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path in ("/galaxy/stop/", "/galaxy/resume/", "/galaxy/status/"):
            return control(request)
        if request.method not in ("GET", "HEAD", "OPTIONS") and not internal_request(
            request
        ):
            with write_lock():
                if read_json(SHUTDOWN).get("phase") in ACTIVE_PHASES:
                    return JsonResponse(
                        {
                            "detail": "The session is being saved. Retry saving or resume editing from the stop dialog."
                        },
                        status=409,
                    )
                return self.get_response(request)
        if (
            request.path == "/"
            and request.method == "GET"
            and request.user.is_authenticated
            and os.getenv("GALAXY_ENTRYPOINT_DOCUMENT", "True").lower()
            in ("true", "1", "yes")
        ):
            startup = read_json(STARTUP)
            if startup.get("phase") == "ready":
                doc = startup.get("document_id")
                return HttpResponseRedirect(
                    f"/document/{doc}/" if doc else "/projects/"
                )
            failed = startup.get("phase") == "failed"
            title = "Import failed" if failed else "Preparing your document"
            refresh = "" if failed else '<meta http-equiv="refresh" content="3">'
            response = HttpResponse(
                '<!doctype html><html><head>'
                f'{refresh}<title>{title}</title>'
                '<style>'
                '.bar { height: 4px; border-radius: 2px; margin-bottom: 1.4rem;'
                '       animation: pulse 1.6s ease-in-out infinite; }'
                '@keyframes pulse { 0%, 100% { background: #e8e3da; }'
                '                   50% { background: #8b1e3f; } }'
                '</style></head>'
                '<body style="font:16px sans-serif;max-width:700px;margin:10vh auto">'
                '<div class="bar" aria-hidden="true"></div>'
                f'<h1>{title}</h1>'
                f'<p>{html.escape(startup.get("message", "Starting your session…"))}</p>'
                '<a href="/projects/">Open projects</a></body></html>'
            )
        else:
            response = self.get_response(request)
        ancestors = frame_ancestors()
        if ancestors is not None:
            # Django's clickjacking middleware sends "X-Frame-Options: DENY",
            # which stops Galaxy from embedding the app in its interactive
            # tool iframe (the Galaxy page and the proxied entry point are
            # different origins). Replace it with a frame-ancestors policy
            # this container controls.
            del response["X-Frame-Options"]
            response["Content-Security-Policy"] = f"frame-ancestors {ancestors}"
        if (
            stop_button_enabled()
            and response.get("Content-Type", "").startswith("text/html")
            and not getattr(response, "streaming", False)
        ):
            get_token(request)
            content = response.content.decode(response.charset)
            marker = content.lower().rfind("</body>")
            if marker >= 0 and BUTTON_ID not in content:
                response.content = (
                    content[:marker] + BUTTON_HTML + content[marker:]
                ).encode(response.charset)
                response["Content-Length"] = str(len(response.content))
        return response
