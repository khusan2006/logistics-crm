import os

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


@register.simple_tag
def static_v(path):
    """Static URL with a ?v=<mtime> cache-buster so browsers refetch on change."""
    url = static(path)
    abs_path = finders.find(path)
    if abs_path:
        try:
            return f"{url}?v={int(os.path.getmtime(abs_path))}"
        except OSError:
            pass
    return url
