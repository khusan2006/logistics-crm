from crm.models import AuditLog


def test_record_writes_row(admin_user):
    AuditLog.record(admin_user, AuditLog.Action.CREATE, "Hamkor", 7, "Yangi hamkor: Pars")
    row = AuditLog.objects.get()
    assert row.user == admin_user and row.target_id == 7 and "Pars" in row.summary


def test_audit_page_admin_only(admin_client, translator_client):
    assert admin_client.get("/audit/").status_code == 200
    assert translator_client.get("/audit/").status_code == 403
