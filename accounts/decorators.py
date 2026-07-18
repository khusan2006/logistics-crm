from functools import wraps

from django.core.exceptions import PermissionDenied


def role_required(*roles):
    """Allow access only to users whose role is in `roles`. Assumes login is already enforced."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if request.user.role not in roles:
                raise PermissionDenied
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator
