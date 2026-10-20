from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.halo_collar.api import HaloState
from custom_components.halo_collar.const import (
    CONF_ALLOW_FENCE_DISABLE,
    CONF_ENABLE_FENCE_CONTROLS,
    CONF_ENABLE_FIND_COLLAR,
    CONF_STALE_AFTER,
)
from custom_components.halo_collar.control_status import ControlReason, control_status_reason
from custom_components.halo_collar.controls import (
    HaloControlError,
    _validate_find_snapshot,
    _validate_snapshot,
    control_stale_after,
)
from custom_components.halo_collar.helpers import pet_for_collar

NOW = datetime(2026, 10, 20, tzinfo=UTC)
OPTIONS = {
    CONF_ENABLE_FENCE_CONTROLS: True,
    CONF_ALLOW_FENCE_DISABLE: True,
    CONF_ENABLE_FIND_COLLAR: True,
}


@pytest.fixture
def snapshot():
    return HaloState(
        collars=[
            {
                "id": "collar-1",
                "petInfo": {"id": "pet-1"},
                "telemetry": {"manifest": {"timestamp": NOW.isoformat()}, "walk": None},
            }
        ],
        pets=[
            {
                "id": "pet-1",
                "collarInfo": {"id": "collar-1", "telemetry": {"walk": None}},
                "isFencesSynchronized": True,
                "telemetry": {"mode": {"fencesOn": True}},
            }
        ],
        subscription={"features": [{"id": "findcollar", "isEnabled": True}]},
        server_time=None,
    )


def reason(snapshot, action="find_collar", **kwargs):
    options = kwargs.pop("options", OPTIONS)
    return control_status_reason(
        snapshot,
        action=action,
        collar_id="collar-1",
        options=options,
        stale_after=control_stale_after(SimpleNamespace(options=options)),
        cloud_success=kwargs.pop("cloud_success", True),
        cooldown_remaining=kwargs.pop("cooldown_remaining", 0),
        now=kwargs.pop("now", NOW),
        **kwargs,
    )


@pytest.mark.parametrize("action", ["enable_fences", "disable_fences", "find_collar"])
def test_status_is_advisory_and_does_not_mutate_cached_evidence(snapshot, action):
    before = deepcopy(snapshot)
    assert reason(snapshot, action) == "ready"
    assert snapshot == before
    snapshot.collars.clear()
    assert reason(snapshot, action, cloud_success=False, options={}) == "disabled"
    assert reason(snapshot, action, cloud_success=False) == "cloud_unavailable"
    assert reason(snapshot, action) == "mapping_unavailable"


@pytest.mark.parametrize("timestamp", [None, "invalid", "2026-10-20T00:00:00"])
def test_unknown_report_does_not_claim_a_stale_report(snapshot, timestamp):
    snapshot.collars[0]["telemetry"]["manifest"]["timestamp"] = timestamp
    assert reason(snapshot) == "report_unknown"


@pytest.mark.parametrize("age", [901, -10000])
def test_invalid_report_age_blocks_controls_even_with_long_connectivity_limit(snapshot, age):
    snapshot.collars[0]["telemetry"]["manifest"]["timestamp"] = (
        NOW - timedelta(seconds=age)
    ).isoformat()
    assert reason(snapshot, options={**OPTIONS, CONF_STALE_AFTER: 86400}) == "report_stale"


def test_shorter_report_limit_is_honored(snapshot):
    assert (
        reason(
            snapshot, now=NOW + timedelta(seconds=121), options={**OPTIONS, CONF_STALE_AFTER: 120}
        )
        == "report_stale"
    )


@pytest.mark.parametrize("action", ["disable_fences", "find_collar"])
def test_walk_unknown_and_active_are_distinct_from_confirmed_no_walk(snapshot, action):
    del snapshot.collars[0]["telemetry"]["walk"]
    assert reason(snapshot, action) == "walk_unknown"
    snapshot.pets[0]["collarInfo"]["telemetry"]["walk"] = {}
    assert reason(snapshot, action) == "walk_active"
    snapshot.collars[0]["telemetry"]["walk"] = None
    snapshot.pets[0]["collarInfo"]["telemetry"]["walk"] = None
    assert reason(snapshot, action) == "ready"


def test_fence_tiers_do_not_share_blockers(snapshot):
    pet = snapshot.pets[0]
    pet["isFencesSynchronized"] = None
    pet["telemetry"]["mode"].clear()
    snapshot.collars[0]["telemetry"]["walk"] = {}
    assert reason(snapshot, "enable_fences") == "ready"
    assert reason(snapshot, "disable_fences") == "fence_sync_unknown"
    pet["isFencesSynchronized"] = False
    assert reason(snapshot, "disable_fences") == "fences_not_synchronized"
    pet["isFencesSynchronized"] = True
    assert reason(snapshot, "disable_fences") == "fence_mode_unknown"
    pet["telemetry"]["mode"]["fencesOn"] = False
    assert reason(snapshot, "disable_fences") == "walk_active"
    assert reason(snapshot, "disable_fences", options={CONF_ENABLE_FENCE_CONTROLS: True}) == (
        "disabled"
    )


@pytest.mark.parametrize("side", ["pet", "collar"])
def test_find_requires_reciprocal_mapping_but_enable_retains_existing_mapping_rules(snapshot, side):
    if side == "pet":
        del snapshot.pets[0]["collarInfo"]["id"]
    else:
        del snapshot.collars[0]["petInfo"]["id"]
    assert reason(snapshot) == "mapping_unavailable"
    assert reason(snapshot, "enable_fences") == "ready"


def test_conflicting_mapping_is_not_ready(snapshot):
    snapshot.pets.append({"id": "pet-2", "collarInfo": {"id": "collar-1"}})
    assert reason(snapshot) == "mapping_unavailable"


@pytest.mark.parametrize(
    "features",
    [
        None,
        [],
        [{"id": "findcollar", "isEnabled": False}],
        [{"id": "findcollar", "isEnabled": True}, {"id": "findcollar", "isEnabled": True}],
    ],
)
def test_find_does_not_claim_entitlement_without_unambiguous_confirmation(snapshot, features):
    snapshot.subscription["features"] = features
    assert reason(snapshot, cooldown_remaining=60) == "entitlement_unconfirmed"


def test_cooldown_is_only_a_find_blocker(snapshot):
    assert reason(snapshot, cooldown_remaining=60) == "cooldown"
    assert reason(snapshot, "enable_fences", cooldown_remaining=60) == "ready"
    assert reason(snapshot, "disable_fences", cooldown_remaining=60) == "ready"
    assert reason(snapshot, cooldown_remaining=0) == "ready"


def test_status_enums_are_translated_and_existing_freshness_entities_keep_names():
    root = Path(__file__).resolve().parents[1] / "custom_components" / "halo_collar"
    strings = json.loads((root / "strings.json").read_text())
    assert strings == json.loads((root / "translations" / "en.json").read_text())
    for action in ("enable_fences", "disable_fences", "find_collar"):
        states = strings["entity"]["sensor"][f"{action}_status"]["state"]
        assert set(states) == set(ControlReason)
        assert states["ready"] == "Ready to try"
    assert strings["entity"]["sensor"]["last_telemetry"]["name"] == "Last telemetry"
    assert strings["entity"]["binary_sensor"]["online"]["name"] == "Connectivity"
    help_text = strings["options"]["step"]["init"]["data_description"]["stale_after"]
    assert "900 seconds" in help_text
    assert "does not force a collar report" in help_text


@pytest.mark.parametrize("action", ["enable_fences", "disable_fences", "find_collar"])
@pytest.mark.parametrize(
    "case",
    [
        "ready",
        "missing_pet",
        "missing_collar",
        "stale",
        "future",
        "report_unknown",
        "walk_unknown",
        "walk_active",
        "sync_unknown",
        "sync_false",
        "mode_unknown",
        "entitlement_unconfirmed",
        "one_sided_mapping",
        "conflicting_mapping",
    ],
)
def test_cached_snapshot_advice_agrees_with_transaction_guards(snapshot, action, case):
    now = datetime.now(UTC)
    pet, collar = snapshot.pets[0], snapshot.collars[0]
    collar["telemetry"]["manifest"]["timestamp"] = now.isoformat()
    if case == "missing_pet":
        snapshot.pets.clear()
    elif case == "missing_collar":
        snapshot.collars.clear()
    elif case in ("stale", "future", "report_unknown"):
        timestamp = {
            "stale": (now - timedelta(seconds=1000)).isoformat(),
            "future": (now + timedelta(seconds=10000)).isoformat(),
            "report_unknown": None,
        }[case]
        collar["telemetry"]["manifest"]["timestamp"] = timestamp
    elif case == "walk_unknown":
        collar["telemetry"].pop("walk")
    elif case == "walk_active":
        collar["telemetry"]["walk"] = {}
    elif case in ("sync_unknown", "sync_false"):
        pet["isFencesSynchronized"] = None if case == "sync_unknown" else False
    elif case == "mode_unknown":
        pet["telemetry"]["mode"].clear()
    elif case == "entitlement_unconfirmed":
        snapshot.subscription["features"] = []
    elif case == "one_sided_mapping":
        pet["collarInfo"].pop("id")
    elif case == "conflicting_mapping":
        snapshot.pets.append({"id": "pet-2", "collarInfo": {"id": "collar-1"}})

    collar = snapshot.collars[0] if snapshot.collars else None
    pet = pet_for_collar(snapshot.pets, collar, snapshot.collars) if collar else None
    limit = control_stale_after(SimpleNamespace(options=OPTIONS))
    try:
        if action == "find_collar":
            _validate_find_snapshot(lambda: (pet, collar, snapshot.subscription), stale_after=limit)
        else:
            _validate_snapshot(
                lambda: (pet, collar), enabled=action == "enable_fences", stale_after=limit
            )
    except HaloControlError:
        accepted = False
    else:
        accepted = True
    assert (reason(snapshot, action, now=now) == "ready") is accepted
