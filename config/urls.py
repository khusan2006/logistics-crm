from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from accounts import views as accounts_views
from crm import views as crm_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", accounts_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", crm_views.dashboard, name="dashboard"),
    path("audit/", crm_views.audit_list, name="audit_list"),
    path("partners/", crm_views.partner_list, name="partner_list"),
    path("partners/new/", crm_views.partner_create, name="partner_create"),
    path("partners/<int:pk>/edit/", crm_views.partner_edit, name="partner_edit"),
    path("partners/<int:pk>/delete/", crm_views.partner_delete, name="partner_delete"),
    path("contracts/", crm_views.contract_list, name="contract_list"),
    path("contracts/new/", crm_views.contract_create, name="contract_create"),
    path("contracts/<int:pk>/edit/", crm_views.contract_edit, name="contract_edit"),
    path("contracts/<int:pk>/delete/", crm_views.contract_delete, name="contract_delete"),
]
