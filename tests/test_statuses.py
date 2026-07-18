import pytest

from crm.models import ShipmentStatus


def test_seed_exists(db):
    names = list(ShipmentStatus.objects.values_list("name", flat=True))
    assert names == ["Tayyorlanmoqda", "Yuklanmoqda", "Yo'lda", "Chegarada", "Bojxona",
                     "Omborga yetib keldi"]
    assert ShipmentStatus.arrival().name == "Omborga yetib keldi"


def test_only_one_arrival(db):
    s = ShipmentStatus.objects.get(name="Bojxona")
    s.is_arrival = True
    s.save()
    assert ShipmentStatus.objects.filter(is_arrival=True).count() == 1
    assert ShipmentStatus.arrival() == s


def test_arrival_delete_blocked(admin_client, db):
    arrival = ShipmentStatus.arrival()
    admin_client.post(f"/statuses/{arrival.pk}/delete/")
    assert ShipmentStatus.objects.filter(pk=arrival.pk).exists()


def test_reorder(admin_client, db):
    first = ShipmentStatus.objects.first()
    admin_client.post(f"/statuses/{first.pk}/move/", {"dir": "down"})
    assert ShipmentStatus.objects.first() != first
