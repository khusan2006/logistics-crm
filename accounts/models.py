from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        TRANSLATOR = "translator", "Tarjimon"

    role = models.CharField("Rol", max_length=12, choices=Role.choices, default=Role.TRANSLATOR)
    phone = models.CharField("Telefon", max_length=30, blank=True)

    @property
    def is_admin_role(self):
        return self.role == self.Role.ADMIN

    def __str__(self):
        return self.get_full_name() or self.username
