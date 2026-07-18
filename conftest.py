"""Shared fixtures: an admin and a translator, plus logged-in test clients."""
import pytest
from django.test import Client

from accounts.models import User

PASSWORD = "test-pass-123"


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(
        username="boss", password=PASSWORD, role=User.Role.ADMIN,
        first_name="Bosh", last_name="Admin",
    )


@pytest.fixture
def translator_user(db):
    return User.objects.create_user(
        username="tarjimon", password=PASSWORD, role=User.Role.TRANSLATOR,
        first_name="Tar", last_name="Jimon",
    )


@pytest.fixture
def admin_client(admin_user):
    client = Client()
    client.force_login(admin_user)
    return client


@pytest.fixture
def translator_client(translator_user):
    client = Client()
    client.force_login(translator_user)
    return client
