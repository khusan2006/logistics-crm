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
    path("customers/", crm_views.customer_list, name="customer_list"),
    path("customers/new/", crm_views.customer_create, name="customer_create"),
    path("customers/<int:pk>/edit/", crm_views.customer_edit, name="customer_edit"),
    path("customers/<int:pk>/delete/", crm_views.customer_delete, name="customer_delete"),
    path("contracts/", crm_views.contract_list, name="contract_list"),
    path("contracts/new/", crm_views.contract_create, name="contract_create"),
    path("contracts/<int:pk>/edit/", crm_views.contract_edit, name="contract_edit"),
    path("contracts/<int:pk>/delete/", crm_views.contract_delete, name="contract_delete"),
    path("supplier-payments/", crm_views.supplier_payment_list, name="supplier_payment_list"),
    path("supplier-payments/new/", crm_views.supplier_payment_create, name="supplier_payment_create"),
    path("supplier-payments/<int:pk>/edit/", crm_views.supplier_payment_edit, name="supplier_payment_edit"),
    path("supplier-payments/<int:pk>/delete/", crm_views.supplier_payment_delete, name="supplier_payment_delete"),
    path("statuses/", crm_views.status_list, name="status_list"),
    path("statuses/new/", crm_views.status_create, name="status_create"),
    path("statuses/<int:pk>/edit/", crm_views.status_edit, name="status_edit"),
    path("statuses/<int:pk>/delete/", crm_views.status_delete, name="status_delete"),
    path("statuses/<int:pk>/move/", crm_views.status_move, name="status_move"),
    path("shipments/", crm_views.shipment_list, name="shipment_list"),
    path("shipments/new/", crm_views.shipment_create, name="shipment_create"),
    path("shipments/<int:pk>/edit/", crm_views.shipment_edit, name="shipment_edit"),
    path("shipments/<int:pk>/status/", crm_views.shipment_set_status, name="shipment_set_status"),
    path("shipments/<int:pk>/delete/", crm_views.shipment_delete, name="shipment_delete"),
    path("shipments/<int:pk>/extend/", crm_views.shipment_extend, name="shipment_extend"),
    path("shipments/<int:pk>/", crm_views.shipment_detail, name="shipment_detail"),
    path("expenses/new/", crm_views.expense_create, name="expense_create"),
    path("expenses/<int:pk>/edit/", crm_views.expense_edit, name="expense_edit"),
    path("expenses/<int:pk>/delete/", crm_views.expense_delete, name="expense_delete"),
]
