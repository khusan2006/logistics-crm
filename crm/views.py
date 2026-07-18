from django.shortcuts import render


def dashboard(request):
    return render(request, "crm/dashboard.html")
