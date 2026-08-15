from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        TRANSLATOR = "translator", "Tarjimon"
        # The person on the shelf: what granula is in the ombor and what left it.
        # They handle the goods, not the deal, so they read Ombor and Sotuvlar and
        # nothing else — and no narx anywhere, which is why every money figure in
        # the app hangs off `is_admin_role` rather than off "is logged in".
        SKLADCHI = "skladchi", "Skladchi"

    role = models.CharField("Rol", max_length=12, choices=Role.choices, default=Role.TRANSLATOR)
    phone = models.CharField("Telefon", max_length=30, blank=True)
    # Kelishuv pk's, in the order this user dragged the dashboard's Kelishuvlar
    # bajarilishi card into. Per user rather than shared: which deal someone wants
    # at the top of their own screen is a reading preference, and one person's
    # drag should not rearrange everyone else's dashboard. A kelishuv missing from
    # the list keeps its automatic rank — see crm.views.dashboard.
    dashboard_contract_order = models.JSONField(default=list, blank=True, editable=False)

    @property
    def is_admin_role(self):
        return self.role == self.Role.ADMIN

    @property
    def is_skladchi(self):
        return self.role == self.Role.SKLADCHI

    def __str__(self):
        return self.get_full_name() or self.username
