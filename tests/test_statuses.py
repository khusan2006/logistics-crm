import pytest

from crm.models import ShipmentStatus


def test_seed_exists(db):
    """The Eron chain, in pipeline order, ending on the shared arrival holat."""
    names = list(ShipmentStatus.for_kind(birja=False).values_list("name", flat=True))
    assert names == ["Tayyorlanmoqda", "Yuklanmoqda", "Yo'lda", "Chegarada", "Bojxona",
                     "Omborga yetib keldi"]
    assert ShipmentStatus.arrival().name == "Omborga yetib keldi"


def test_the_birja_chain_is_its_own_and_ends_on_the_same_arrival_holat(db):
    """Two pipelines, one ending. The placeholder birja steps are the operator's to
    rename; what must hold is that they are separate from the Eron ones and that
    both finish on the row the ombor keys off."""
    names = list(ShipmentStatus.for_kind(birja=True).values_list("name", flat=True))
    assert names == ["Sotib olindi", "Yuklandi", "Yetkazilmoqda", "Omborga yetib keldi"]
    assert ShipmentStatus.arrival().scope == ShipmentStatus.Scope.BOTH


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
