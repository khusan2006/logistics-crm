from accounts.models import User


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
