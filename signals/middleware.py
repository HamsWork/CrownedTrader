from django.shortcuts import redirect
from django.urls import reverse

from .models import Agreement


class AgreementRequiredMiddleware:
    """
    Forces users to accept the current Agreement before using the app.

    - Shows on first login (no acceptance)
    - Shows again when Agreement version changes
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not getattr(request, "user", None) or not request.user.is_authenticated:
            return self.get_response(request)

        # Allow agreement + auth endpoints and static assets
        path = request.path or ""
        try:
            agreement_path = reverse("agreement")
        except Exception:
            agreement_path = "/agreement/"

        allowed_prefixes = ("/static/", "/login/", "/logout/", agreement_path, "/admin/")
        if any(path.startswith(p) for p in allowed_prefixes):
            return self.get_response(request)

        current = Agreement.objects.filter(is_active=True).order_by("-published_at", "-id").first()
        if not current:
            return self.get_response(request)

        accepted = (
            current.acceptances.filter(user_id=request.user.id).order_by("-accepted_at", "-id").first()
            if request.user.id
            else None
        )
        if accepted:
            return self.get_response(request)

        try:
            next_q = request.get_full_path()
        except Exception:
            next_q = path
        return redirect(f"{agreement_path}?next={next_q}")

