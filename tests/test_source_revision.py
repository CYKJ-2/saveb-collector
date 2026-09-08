from copy import deepcopy

import pytest

from app.persistence.orders import source_revision_payload


def source():
    return {
        "order": {"amount": "12", "paymentStatus": "Completed", "remark": "old"},
        "productList": [{"productId": "p1", "number": 2, "productImg": "https://www.example/a"}],
    }


def test_presentation_changes_do_not_mutate_evidence():
    original = source()
    incoming = deepcopy(original)
    incoming["order"]["remark"] = "new"
    incoming["productList"][0]["productImg"] = "https://img.example/b"
    evidence = deepcopy(incoming)
    assert source_revision_payload(original) == source_revision_payload(incoming)
    assert incoming == evidence
    assert original == source()


@pytest.mark.parametrize("field,value", [("productId", "p2"), ("number", 3)])
def test_image_exception_preserves_product_guards(field, value):
    original = source()
    incoming = deepcopy(original)
    incoming["productList"][0].update(productImg="https://img.example/b", **{field: value})
    assert source_revision_payload(original) != source_revision_payload(incoming)


@pytest.mark.parametrize("field,value", [("amount", "55"), ("paymentStatus", "Refunded")])
def test_image_exception_preserves_order_guards(field, value):
    original = source()
    incoming = deepcopy(original)
    incoming["productList"][0]["productImg"] = "https://img.example/b"
    incoming["order"][field] = value
    assert source_revision_payload(original) != source_revision_payload(incoming)
