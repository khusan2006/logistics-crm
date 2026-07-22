from accounts.models import User


def test_userform_rejects_bad_phone(db):
    from accounts.forms import UserForm
    f = UserForm({"username": "u1", "first_name": "A", "last_name": "B",
                  "phone": "12345", "role": User.Role.TRANSLATOR, "password": "secret12"})
    assert not f.is_valid() and "phone" in f.errors


def test_userform_accepts_intl_phone(db):
    from accounts.forms import UserForm
    f = UserForm({"username": "u2", "first_name": "A", "last_name": "B",
                  "phone": "+998 90 123 45 67", "role": User.Role.TRANSLATOR, "password": "secret12"})
    assert f.is_valid(), f.errors


def test_roles_exist(db):
    u = User.objects.create_user(username="x", password="p", role=User.Role.TRANSLATOR)
    assert not u.is_admin_role
    assert User.objects.create_user(username="y", password="p", role=User.Role.ADMIN).is_admin_role


def test_login_required_redirects(client, db):
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.url.startswith("/login/")


def test_admin_sees_dashboard(admin_client):
    resp = admin_client.get("/")
    assert resp.status_code == 200
    assert "GranulaLog" in resp.content.decode()
