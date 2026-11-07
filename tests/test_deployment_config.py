"""Structural guards for the self-hosted deployment files.

These tests encode the mistakes issue #96 calls out: a scheduler without the
collector's permission environment, a missing FreshRSS healthcheck, a Docker
socket mount, or an image-level ENTRYPOINT that turns the collector override
into arguments to supercronic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = (
    "compose.yml",
    "compose.production.yml",
    "compose.mcp.yml",
    "docker-compose.yml",
)


def load_compose(name: str) -> dict:
    return yaml.safe_load((REPO_ROOT / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def production() -> dict:
    return load_compose("compose.production.yml")


@pytest.fixture(scope="module")
def mcp() -> dict:
    return load_compose("compose.mcp.yml")


def test_scheduler_is_a_long_running_service(production: dict) -> None:
    scheduler = production["services"]["scheduler"]

    assert "profiles" not in scheduler
    assert scheduler["restart"] == "unless-stopped"
    assert scheduler["entrypoint"] == ["/usr/local/bin/dailyinfo-scheduler"]
    assert "ports" not in scheduler


def test_scheduler_uses_the_same_image_as_the_collector(production: dict) -> None:
    assert (
        production["services"]["scheduler"]["image"]
        == production["services"]["collector"]["image"]
    )


def test_scheduler_inherits_the_collector_permission_environment(
    production: dict,
) -> None:
    scheduler_env = production["services"]["scheduler"]["environment"]
    collector_env = production["services"]["collector"]["environment"]

    for key in (
        "TZ",
        "DAILYINFO_ENV",
        "DAILYINFO_DATA_ROOT",
        "FRESHRSS_USER",
        "USER",
        "DAILYINFO_PUBLICATION_GROUP_READ",
    ):
        assert scheduler_env[key] == collector_env[key], key


def test_scheduler_crontab_path_is_visible_to_healthchecks(production: dict) -> None:
    env = production["services"]["scheduler"]["environment"]

    # Docker healthchecks and `compose exec` do not inherit the entrypoint's
    # exports, so this must be part of the static environment.
    assert env["DAILYINFO_SCHEDULER_CRONTAB"].startswith("/etc/dailyinfo/crontab.")


def test_scheduler_waits_for_a_healthy_freshrss(production: dict) -> None:
    scheduler = production["services"]["scheduler"]
    freshrss = production["services"]["freshrss"]

    assert "healthcheck" in freshrss
    assert scheduler["depends_on"]["freshrss"]["condition"] == "service_healthy"


def test_scheduler_healthcheck_uses_the_wrapper(production: dict) -> None:
    test = production["services"]["scheduler"]["healthcheck"]["test"]

    assert "scripts.scheduler" in test
    assert "healthcheck" in test


def test_collector_takes_the_scheduler_lock(production: dict) -> None:
    command = production["services"]["collector"]["command"]

    assert "scripts.scheduler" in command
    assert "exec" in command
    assert command.index("exec") < command.index("scripts.cli")


def test_scheduler_has_a_separate_optional_push_env(production: dict) -> None:
    scheduler_files = production["services"]["scheduler"]["env_file"]
    collector_files = production["services"]["collector"]["env_file"]

    assert len(scheduler_files) == 2
    assert any(
        isinstance(entry, dict) and "DAILYINFO_PUSH_ENV" in entry["path"]
        for entry in scheduler_files
    )
    assert len(collector_files) == 1
    assert "DAILYINFO_PUSH_ENV" not in str(collector_files)


def test_scheduler_never_mounts_the_mcp_key_directory(production: dict) -> None:
    volumes = production["services"]["scheduler"]["volumes"]

    assert not any("/run/secrets/dailyinfo" in str(volume) for volume in volumes)


def test_no_service_mounts_the_docker_socket() -> None:
    for name in COMPOSE_FILES:
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "docker.sock" not in text, name


def test_producer_image_has_no_entrypoint() -> None:
    text = (REPO_ROOT / "Dockerfile.producer").read_text(encoding="utf-8")
    instructions = [
        line.strip().upper()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    assert not any(line.startswith("ENTRYPOINT") for line in instructions)
    assert "tzdata" in text
    assert "supercronic" in text
    assert "--no-dev" in text


def test_compose_entrypoint_includes_both_stacks() -> None:
    compose = load_compose("compose.yml")

    assert compose["name"] == "dailyinfo"
    assert compose["include"] == ["compose.production.yml", "compose.mcp.yml"]


def test_legacy_compose_is_dev_scoped() -> None:
    legacy = load_compose("docker-compose.yml")
    freshrss = legacy["services"]["freshrss"]

    assert legacy["name"] == "dailyinfo-dev"
    assert freshrss["container_name"] == "dailyinfo-freshrss-dev"
    assert "dailyinfo-dev" in freshrss["volumes"][0]
    assert "dailyinfo_dev_net" in legacy["networks"]


def test_mcp_stays_read_only(mcp: dict) -> None:
    service = mcp["services"]["dailyinfo-mcp"]

    assert service["read_only"] is True
    assert service["user"] == "10001:10001"
