import pytest

from xcodon_runtime.reference import (
    Platform,
    Reference,
    host_platform,
    parse_platform,
    parse_reference,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("python:3.12", Reference("docker.io", "library/python", "3.12", None)),
        ("python", Reference("docker.io", "library/python", "latest", None)),
        ("xcodon/foo", Reference("docker.io", "xcodon/foo", "latest", None)),
        (
            "quay.io/biocontainers/samtools:1.20--h50ea8bc_0",
            Reference("quay.io", "biocontainers/samtools", "1.20--h50ea8bc_0", None),
        ),
        ("localhost:5000/img:v1", Reference("localhost:5000", "img", "v1", None)),
        ("ghcr.io/org/app", Reference("ghcr.io", "org/app", "latest", None)),
        (
            "xcodon/10-1101_2025-06-17-659900_v1-python-deps:latest",
            Reference("docker.io", "xcodon/10-1101_2025-06-17-659900_v1-python-deps", "latest", None),
        ),
        ("docker://alpine:3.19", Reference("docker.io", "library/alpine", "3.19", None)),
    ],
)
def test_parse_reference(text, expected):
    assert parse_reference(text) == expected


def test_digest_reference():
    d = "sha256:" + "a" * 64
    ref = parse_reference(f"alpine@{d}")
    assert ref == Reference("docker.io", "library/alpine", None, d)
    assert ref.manifest_ref == d


def test_tag_and_digest_prefers_digest():
    d = "sha256:" + "b" * 64
    ref = parse_reference(f"alpine:3.19@{d}")
    assert ref.tag == "3.19"
    assert ref.manifest_ref == d


def test_name_round_trip():
    assert parse_reference("python:3.12").name == "docker.io/library/python:3.12"
    assert parse_reference("quay.io/a/b").name == "quay.io/a/b:latest"


def test_api_host_for_docker_hub():
    assert parse_reference("python").api_host == "registry-1.docker.io"
    assert parse_reference("quay.io/a/b").api_host == "quay.io"


@pytest.mark.parametrize("bad", ["", "Upper/case", "a@sha256:short", "a:b:c/d"])
def test_invalid_reference(bad):
    with pytest.raises(ValueError):
        parse_reference(bad)


def test_platform_parsing():
    assert parse_platform("linux/amd64") == Platform("linux", "amd64", None)
    assert parse_platform("linux/arm64/v8") == Platform("linux", "arm64", "v8")
    assert host_platform().os == "linux"
    assert host_platform().architecture in {"amd64", "arm64"}
