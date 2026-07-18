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
]
