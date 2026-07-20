import pytest

from routeforge import parse_seed_list, seed_sweep_payload


def test_parse_seed_list() -> None:
    assert parse_seed_list("17, 29,41") == (17, 29, 41)
    with pytest.raises(ValueError):
        parse_seed_list("17,17")
    with pytest.raises(ValueError):
        parse_seed_list("17,")


def test_sweep_reports_stability_and_feasibility() -> None:
    payload = seed_sweep_payload((17, 29, 41))
    assert payload["seeds"] == [17, 29, 41]
    assert len(payload["runs"]) == 3
    assert payload["aggregate"]["compactFeasibleRate"]["mean"] == 1.0
    assert payload["aggregate"]["stressFeasibleRate"]["mean"] == 1.0


def test_sweep_is_deterministic_and_rejects_bad_inputs() -> None:
    assert seed_sweep_payload((17, 29, 41)) == seed_sweep_payload((17, 29, 41))
    with pytest.raises(ValueError):
        seed_sweep_payload(())
    with pytest.raises(ValueError):
        seed_sweep_payload((True,))
