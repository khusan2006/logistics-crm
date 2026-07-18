"""Helpers for rendering create/edit forms either full-page or inside a modal."""

from django.http import HttpResponse
from django.shortcuts import redirect, render


def is_ajax(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def form_response(request, form, title, invalid=False, modal_template="_modal.html", extra_context=None):
    """Render the modal partial for AJAX requests, otherwise the full page."""
    context = {"form": form, "title": title}
    if extra_context:
        context.update(extra_context)
    if is_ajax(request):
        status = 422 if invalid else 200
        return render(request, modal_template, context, status=status)
    return render(request, "crm/form.html", context)


def form_success(request, url):
    """Tell an AJAX modal to redirect; otherwise do a normal redirect."""
    if is_ajax(request):
        response = HttpResponse(status=204)
        response["X-Redirect"] = url
        return response
    return redirect(url)


def form_reload(request, fallback_url):
    """For in-place actions (edit/delete/settle): an AJAX modal reloads the page
    it was opened from (204 with no X-Redirect); otherwise redirect to a fallback."""
    if is_ajax(request):
        return HttpResponse(status=204)
    return redirect(fallback_url)


def render_confirm(request, title, message, confirm_label, confirm_class="", cancel_url_name="dashboard"):
    """Render a confirm dialog as a modal partial (AJAX) or a full page."""
    context = {
        "title": title,
        "message": message,
        "confirm_label": confirm_label,
        "confirm_class": confirm_class,
        "cancel_url_name": cancel_url_name,
    }
    template = "_confirm_modal.html" if is_ajax(request) else "crm/confirm.html"
    return render(request, template, context)
