"""Node auto-discovery.

The application must start with no arguments, which means finding a node rather
than being told where one is. These tests cover the awkward cases: unreadable
service datadirs, missing directories, and credentials that exist but are wrong.
"""

import os
import stat
from pathlib import Path

import pytest

from arcade.discovery import Candidate, _exists, discover, explain, probe


def test_missing_datadir_is_not_an_error(tmp_path):
    result = probe(tmp_path / "nope", "test")
    assert not result.reachable
    assert result.problems


def test_unreadable_directory_does_not_raise(tmp_path):
    """A service datadir is typically 0710; stat() inside it raises for us.

    Discovery must treat that as "look elsewhere", not as a crash -- this is the
    normal state of /var/lib/pepecoind and it broke the first implementation.
    """
    locked = tmp_path / "locked"
    (locked / "testnet3").mkdir(parents=True)
    (locked / "testnet3" / ".cookie").write_text("user:pass")
    os.chmod(locked, 0o000)
    try:
        assert _exists(locked / "testnet3" / ".cookie") is False
        result = probe(locked, "test")          # must not raise
        assert not result.reachable
    finally:
        os.chmod(locked, 0o700)


def test_cookie_is_preferred_over_config(tmp_path):
    """A cookie is regenerated every start, so it can never be stale."""
    (tmp_path / "testnet3").mkdir(parents=True)
    (tmp_path / "testnet3" / ".cookie").write_text("cookieuser:cookiepass")
    (tmp_path / "pepecoin.conf").write_text("rpcuser=confuser\nrpcpassword=confpass\n")

    result = probe(tmp_path, "test")
    assert result.source == "cookie"
    assert result.credentials.user == "cookieuser"


def test_config_is_used_when_the_cookie_is_unreadable(tmp_path):
    """How a node running as its own user is reached at all."""
    (tmp_path / "testnet3").mkdir(parents=True)
    (tmp_path / "pepecoin.conf").write_text("rpcuser=confuser\nrpcpassword=confpass\n")

    result = probe(tmp_path, "test")
    assert result.source.startswith("config")
    assert result.credentials.user == "confuser"


def test_credentials_use_the_network_port_not_the_config_port(tmp_path):
    """A bare rpcport in someone else's config must not redirect us.

    This is the bug that silently pointed a testnet command at the mainnet node.
    """
    (tmp_path / "testnet3").mkdir(parents=True)
    (tmp_path / "pepecoin.conf").write_text(
        "rpcuser=u\nrpcpassword=p\nrpcport=33873\n"      # mainnet port
    )
    result = probe(tmp_path, "test")
    assert result.credentials.port == 44873, "must use the testnet port"


def test_explain_lists_what_was_tried(tmp_path):
    text = explain("test", extra=tmp_path)
    assert "Tried" in text or "No Pepecoin datadir" in text


def test_discover_returns_reachable_candidates_first():
    results = discover("test")
    if len(results) > 1:
        flags = [c.reachable for c in results]
        assert flags == sorted(flags, reverse=True)


# --- both datadirs, on every platform -----------------------------------------
# A Windows user with a synced testnet node was told "Testnet NOT FOUND" while
# its RPC port answered perfectly well: the installer writes testnet to a
# sibling of the mainnet directory on every platform, and discovery only knew
# the Linux spelling of that. Two places encoding one convention, and only one
# of them kept up.


@pytest.mark.parametrize("system,appdata,expected", [
    ("Windows", r"C:\Users\Darrell\AppData\Roaming", "Pepecoin-testnet"),
    ("Darwin", None, "Pepecoin-testnet"),
    ("Linux", None, ".pepecoin-testnet"),
])
def test_the_testnet_datadir_is_looked_for_on_every_platform(
        monkeypatch, system, appdata, expected):
    from arcade import discovery

    monkeypatch.setattr(discovery.platform, "system", lambda: system)
    if appdata:
        monkeypatch.setenv("APPDATA", appdata)
    found = [str(p) for p in discovery._platform_datadirs()]
    assert any(p.endswith(expected) for p in found), found
    assert len(found) >= 2, "mainnet and testnet, both"


def test_discovery_agrees_with_what_the_installer_writes(monkeypatch):
    """The two must not drift again: whatever the installer creates is exactly
    what discovery has to look in."""
    import pathlib as _pathlib
    import sys

    sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent / "installer"))
    import install

    from arcade import discovery

    for system in ("Windows", "Darwin", "Linux"):
        monkeypatch.setattr(discovery.platform, "system", lambda s=system: s)
        written = {str(p) for p in install.datadirs(system, install.PEPECOIN)}
        looked_in = {str(p) for p in discovery._platform_datadirs()}
        assert written <= looked_in, (
            f"{system}: the installer writes {written - looked_in} and nothing "
            f"looks there")
