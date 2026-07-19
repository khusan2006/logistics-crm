from accounts.models import User


def test_admin_creates_translator(admin_client):
    resp = admin_client.post("/users/new/", {
        "username": "yangi_tarjimon", "first_name": "Yangi", "last_name": "Tarjimon",
        "phone": "+998 90 000 00 00", "role": User.Role.TRANSLATOR, "password": "s3cret-pass",
    })
    assert resp.status_code == 302
    user = User.objects.get(username="yangi_tarjimon")
    assert user.role == User.Role.TRANSLATOR
    assert user.check_password("s3cret-pass")
    assert user.password != "s3cret-pass"


def test_admin_edits_user_role_and_password_optional(admin_client, translator_user):
    resp = admin_client.post(f"/users/{translator_user.pk}/edit/", {
        "username": translator_user.username, "first_name": translator_user.first_name,
        "last_name": translator_user.last_name, "phone": "", "role": User.Role.ADMIN, "password": "",
    })
    assert resp.status_code in (200, 302, 204)
    translator_user.refresh_from_db()
    assert translator_user.role == User.Role.ADMIN
    # password unchanged (old password still works since we left the field blank)
    assert translator_user.check_password("test-pass-123")


def test_translator_forbidden(translator_client):
    assert translator_client.get("/users/").status_code == 403
    assert translator_client.post("/users/new/", {}).status_code == 403


def test_admin_can_list_users(admin_client):
    resp = admin_client.get("/users/")
    assert resp.status_code == 200
