"""The update command, which refused to run on the machine it was run from.

`dogecoinarcade-update` assumed the installer's layout -- ~/.dogecoinarcade/venv
and .../src -- and raised "no installation found" on anything else. One of the
two machines running this was set up from a source checkout, so the documented
command failed there with nothing but that message, and the shim was not even on
PATH to produce it: `dogecoinarcade-update: command not found`.

A source checkout has no code to fetch. What it does still need is the half of
an update that is about the machine: the service registered, unit files brought
current, and the interface restarted so it stops serving the old code from
memory. These cover that split.
"""

from pathlib import Path

import json

import pytest

from arcade import update


@pytest.fixture
def checkout(tmp_path):
    """Something shaped enough like a checkout for _layout to accept it."""
    root = tmp_path / "src"
    (root / "arcade").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='dogecoinarcade'\n")
    return root


def test_the_installer_layout_still_wins(tmp_path, monkeypatch):
    home = tmp_path / ".dogecoinarcade"
    (home / "venv").mkdir(parents=True)
    monkeypatch.setattr(update, "HOME", home)

    venv, source, fetchable = update._layout()
    assert venv == home / "venv"
    assert source == home / "src"
    assert fetchable, "an installer install is the one that can be fetched into"


def test_a_source_checkout_is_found_and_is_not_fetchable(tmp_path, monkeypatch, checkout):
    """The fix: it locates itself instead of refusing."""
    monkeypatch.setattr(update, "HOME", tmp_path / "absent")
    monkeypatch.setattr(update, "__file__", str(checkout / "arcade" / "update.py"))
    monkeypatch.setattr(update.sys, "prefix", str(checkout / ".venv"))

    venv, source, fetchable = update._layout()
    assert source == checkout
    assert venv == checkout / ".venv"
    assert not fetchable, (
        "a checkout has nothing to fetch -- on the machine releases are published "
        "from it is ahead of what is published, and pulling would try to rewind it"
    )


def test_neither_layout_is_an_error_that_names_both(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "HOME", tmp_path / "absent")
    monkeypatch.setattr(update, "__file__", str(tmp_path / "nowhere" / "arcade" / "update.py"))

    with pytest.raises(update.UpdateError) as raised:
        update._layout()
    message = str(raised.value)
    assert "no installation found" in message
    assert "checkout" in message


def test_a_source_checkout_never_reaches_git(tmp_path, monkeypatch, checkout, capsys):
    """It used to raise before this point; it must not start pulling either."""
    monkeypatch.setattr(update, "HOME", tmp_path / "absent")
    monkeypatch.setattr(update, "__file__", str(checkout / "arcade" / "update.py"))
    monkeypatch.setattr(update.sys, "prefix", str(checkout / ".venv"))

    def no_git():
        raise AssertionError("a source checkout must not be fetched into")

    monkeypatch.setattr(update, "_git", no_git)
    monkeypatch.setattr(update, "_run", lambda *a, **k: pytest.fail("no subprocesses"))
    monkeypatch.setattr(update.shutil, "which", lambda name: None)
    monkeypatch.setattr(update, "_call_installer", lambda *a: None)

    assert update.update(dry_run=False) == 0
    out = capsys.readouterr().out
    assert "Source checkout" in out
    assert "Nothing to fetch" in out
    assert "wallet, messages and chain data were not touched" in out


def test_the_launcher_is_written_when_it_is_missing(tmp_path, monkeypatch, checkout):
    """The `command not found` half: a machine can be running this and have no shim."""
    bindir = tmp_path / "bin"
    calls = []

    def fake_installer(_checkout, function, *args):
        calls.append(function)
        if function == "bindir":
            return bindir
        if function == "shim_names":
            return ["dogecoinarcade", "dogecoinarcade-update", "arcade-rpc"]
        if function == "shims_current":
            return all((Path(args[0]) / name).exists() for name in
                       ("dogecoinarcade", "dogecoinarcade-update", "arcade-rpc"))
        if function == "write_launcher":
            target = Path(args[1])
            target.mkdir(parents=True, exist_ok=True)
            for name in ("dogecoinarcade", "dogecoinarcade-update", "arcade-rpc"):
                (target / name).write_text("#!/bin/sh\n")
            return target / "dogecoinarcade"
        return None

    monkeypatch.setattr(update, "_call_installer", fake_installer)

    written = update._ensure_launcher(checkout, checkout / ".venv")
    assert written == bindir / "dogecoinarcade"
    assert "write_launcher" in calls

    # Idempotent: a second run must not rewrite a shim that is already there.
    calls.clear()
    assert update._ensure_launcher(checkout, checkout / ".venv") is None
    assert "write_launcher" not in calls

    # A machine installed before `arcade-rpc` existed has the first two and not
    # the third; that is exactly the `command not found` this is for.
    (bindir / "arcade-rpc").unlink()
    calls.clear()
    assert update._ensure_launcher(checkout, checkout / ".venv") == bindir / "dogecoinarcade"
    assert "write_launcher" in calls
    assert (bindir / "arcade-rpc").exists()


def test_check_reads_the_checkout_it_actually_has(tmp_path, monkeypatch, checkout):
    """It reported "installed: unknown, an update is available" on a current machine.

    `check` looked in the installer's ~/.dogecoinarcade/src, which a source
    install does not have, so every check claimed the machine was behind.
    """
    monkeypatch.setattr(update, "HOME", tmp_path / "absent")
    monkeypatch.setattr(update, "__file__", str(checkout / "arcade" / "update.py"))
    monkeypatch.setattr(update.sys, "prefix", str(checkout / ".venv"))
    monkeypatch.setattr(update, "current_revision",
                        lambda path: "abc1234" if path == checkout else None)
    monkeypatch.setattr(update, "remote_revision", lambda channel=None: "abc1234")

    installed, published, available = update.check()
    assert installed == "abc1234"
    assert published == "abc1234"
    assert not available, "the machine is on the published commit"


# --- two units can be called arcade-web ---------------------------------------
#
# This machine already had a hardened system unit at
# /etc/systemd/system/arcade-web.service. `ensure_web_service` checked only the
# USER path, so it wrote a second, user-level unit -- which could never bind,
# because the system one owned :8420. The user unit sat in a restart loop
# (NRestarts reached 40) while the system service carried on serving code from
# hours earlier, and every `dogecoinarcade-update` restarted the user unit and
# printed "restarted (user service)". Nothing in the output was false; it was
# just about the wrong unit.
#
# The interface's own staleness banner was the only thing telling the truth, and
# I did not read it -- I grepped the page for the commit hash instead, which
# matched the banner's "<hash> is installed" text. That is the day's lesson
# again: presence of a string is not the property you wanted.


def test_the_owner_of_the_port_is_restarted_first(monkeypatch):
    """Not the first scope that accepts the command."""
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        import subprocess
        # The user unit exists but is NOT the listener; the system unit is.
        if args[:2] == ("systemctl", "--user") and "show" in args:
            return subprocess.CompletedProcess(args, 0, "99999\n", "")
        if args[0] == "systemctl" and "show" in args:
            return subprocess.CompletedProcess(args, 0, "4242\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(update, "_run", fake_run)
    monkeypatch.setattr(update, "_listener_pid", lambda port=8420: 4242)

    scopes = update._scopes_for_port()
    assert scopes[0] == [], (
        "the system unit holds the port, so it must be restarted first"
    )


def test_a_scope_that_owns_nothing_is_still_offered_last(monkeypatch):
    """So a machine with no listener yet can still be restarted."""
    import subprocess

    monkeypatch.setattr(update, "_listener_pid", lambda port=8420: None)
    monkeypatch.setattr(update, "_run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 0, "", ""))

    scopes = update._scopes_for_port()
    assert scopes == [["--user"], []], "both scopes exist, neither owns the port"


def test_the_second_half_runs_in_the_code_just_installed(tmp_path, monkeypatch, capsys):
    """An updater fix took effect one update late: the process that pip had just
    replaced on disk carried on running the copy it had imported, so the shim
    the new code knew to write was written by the *next* run. After the
    reinstall, the rest of the update is handed to the fresh install."""
    home = tmp_path / ".dogecoinarcade"
    venv = home / "venv"
    src = home / "src"
    (venv / "bin").mkdir(parents=True)
    (src / ".git").mkdir(parents=True)
    (src / "arcade").mkdir()
    (src / "pyproject.toml").write_text("[project]\nname='dogecoinarcade'\n")
    monkeypatch.setattr(update, "HOME", home)
    monkeypatch.setattr(update, "_find_git", lambda: "git")
    monkeypatch.setattr(update, "current_revision", lambda _c: "abc1234")

    class Done:
        returncode = 0
        stdout = stderr = ""

    ran = []
    monkeypatch.setattr(update, "_run", lambda *a, **k: ran.append(a) or Done())
    handed_off = []
    monkeypatch.setattr(update.subprocess, "run",
                        lambda argv, **k: handed_off.append(argv) or Done())
    monkeypatch.setattr(update, "_update_services",
                        lambda *a: pytest.fail("the old copy must not run the services half"))

    assert update.update(dry_run=False) == 0
    assert any(a[1:3] == ("install", "-q") for a in ran), ran
    python = str(venv / "bin" / "python")
    assert handed_off == [[python, "-c", "import arcade"],
                          [python, "-m", "arcade.update", "--services-only"]]
    assert "installed" in capsys.readouterr().out


def test_services_only_is_the_second_half_and_installs_nothing(tmp_path, monkeypatch):
    home = tmp_path / ".dogecoinarcade"
    venv = home / "venv"
    src = home / "src"
    (venv / "bin").mkdir(parents=True)
    (src / "arcade").mkdir(parents=True)
    (src / "pyproject.toml").write_text("[project]\nname='dogecoinarcade'\n")
    monkeypatch.setattr(update, "HOME", home)
    monkeypatch.setattr(update, "_run", lambda *a, **k: pytest.fail("nothing to fetch or install"))
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: pytest.fail("no third half"))
    seen = []
    monkeypatch.setattr(update, "_update_services",
                        lambda checkout, v, dry_run: seen.append((checkout, v, dry_run)) or 0)

    assert update.main(["--services-only"]) == 0
    assert seen == [(src, venv, False)]


def test_a_reinstall_that_leaves_nothing_importable_says_so(tmp_path, monkeypatch):
    """pip removes the old version first, so a failure there empties the venv.

    Handing the rest of the update to an environment with no application in it
    produced ModuleNotFoundError -- a traceback about a module, for a machine
    whose application had just been deleted.
    """
    home = tmp_path / ".dogecoinarcade"
    venv = home / "venv"
    src = home / "src"
    (venv / "bin").mkdir(parents=True)
    (src / ".git").mkdir(parents=True)
    (src / "pyproject.toml").write_text("[project]\nname='dogecoinarcade'\n")
    monkeypatch.setattr(update, "HOME", home)
    monkeypatch.setattr(update, "_find_git", lambda: "git")
    monkeypatch.setattr(update, "current_revision", lambda _c: "abc1234")

    class Done:
        returncode = 0
        stdout = stderr = ""

    class Missing:
        returncode = 1
        stdout = stderr = ""

    monkeypatch.setattr(update, "_run", lambda *a, **k: Done())
    monkeypatch.setattr(update.subprocess, "run", lambda argv, **k: Missing())

    with pytest.raises(update.UpdateError) as exc:
        update.update(dry_run=False)
    assert "no longer imports" in str(exc.value)
    assert "repair.py" in str(exc.value), "say the command that fixes it"


def test_an_unsigned_release_is_not_installed(tmp_path, monkeypatch):
    """The whole point of the manifest: without it, nothing is installed.

    An updater that installs code it cannot attribute is a website with a
    shell on every machine that ever ran this. That was survivable while a
    person typed the command; it is not, now that the machine types it.
    """
    import urllib.error

    def no_manifest(*a, **k):
        raise urllib.error.URLError("404")

    monkeypatch.setattr(update.urllib.request, "urlopen", no_manifest)
    with pytest.raises(update.UpdateError) as exc:
        update._signed_manifest()
    assert "Nothing was installed" in str(exc.value)


def test_a_release_signed_by_somebody_else_is_not_installed(tmp_path, monkeypatch):
    from nacl.signing import SigningKey
    from arcade import release

    attacker = SigningKey.generate()
    manifest = release.sign(attacker.encode().hex(), "bad1234", "ff" * 32)

    class Answer:
        def read(self):
            return json.dumps(manifest).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(update.urllib.request, "urlopen", lambda *a, **k: Answer())
    with pytest.raises(update.UpdateError, match="not signed by the key"):
        update._signed_manifest()


def test_the_archive_must_hash_to_what_was_signed(tmp_path, monkeypatch):
    """The signature covers the hash, and the fetch is handed that hash, so
    the site cannot serve a different archive than the one that was signed."""
    from nacl.signing import SigningKey
    from arcade import release

    signer = SigningKey.generate()
    manifest = release.sign(signer.encode().hex(), "abc1234", "de" * 32)
    monkeypatch.setattr(release, "PUBLIC_KEY", signer.verify_key.encode().hex())

    class Answer:
        def read(self):
            return json.dumps(manifest).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(update.urllib.request, "urlopen", lambda *a, **k: Answer())
    monkeypatch.setattr(update, "HOME", tmp_path)
    signed = update._signed_manifest()
    assert signed["sha256"] == "de" * 32

    # And once it is installed, an older signed release is refused.
    update._remember(signed)
    older = release.sign(signer.encode().hex(), "old1234", "aa" * 32, published=1.0)
    manifest.clear(); manifest.update(older)
    with pytest.raises(update.UpdateError, match="downgrade"):
        update._signed_manifest()


def _git(*args, cwd):
    import subprocess
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                           "GIT_COMMITTER_NAME": "t",
                                           "GIT_COMMITTER_EMAIL": "t@t",
                                           "PATH": "/usr/bin:/bin"}).stdout.strip()


def _published(tmp_path, text):
    """A published repository with one commit saying `text`, on master."""
    src = tmp_path / f"src-{abs(hash(text))}"
    src.mkdir()
    _git("init", "-q", "-b", "master", cwd=src)
    (src / "a.txt").write_text(text)
    _git("add", "a.txt", cwd=src)
    _git("commit", "-q", "-m", text, cwd=src)
    return src


def test_a_clean_checkout_follows_a_rewritten_history(tmp_path):
    """The published copy is filtered before it goes out; when the filter
    changes, the history is rewritten and a fast-forward cannot follow it."""
    import shutil
    pub = _published(tmp_path, "first, with a name in it")
    mine = tmp_path / "checkout"
    _git("clone", "-q", str(pub), str(mine), cwd=tmp_path)
    shutil.rmtree(pub)
    _published(tmp_path, "first, without").rename(pub)
    assert update._resync("git", mine)
    assert (mine / "a.txt").read_text() == "first, without"


def test_a_checkout_with_local_edits_is_left_alone(tmp_path):
    import shutil
    pub = _published(tmp_path, "one")
    mine = tmp_path / "checkout"
    _git("clone", "-q", str(pub), str(mine), cwd=tmp_path)
    (mine / "a.txt").write_text("my own change")
    shutil.rmtree(pub)
    _published(tmp_path, "two").rename(pub)
    assert not update._resync("git", mine)
    assert (mine / "a.txt").read_text() == "my own change"


# --- main live or main test (2026-10-28, Team HQ: "admin toggle for selecting main live vs main test") ----------

def _choose(home, channel):
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps({update.CHANNEL_SETTING: channel}))


def test_the_channel_is_live_unless_test_was_picked(tmp_path, monkeypatch):
    home = tmp_path / ".dogecoinarcade"
    monkeypatch.setattr(update, "HOME", home)
    assert update.chosen_channel() == "live", "no settings yet"
    _choose(home, "test")
    assert update.chosen_channel() == "test"
    _choose(home, "nightly-ish")
    assert update.chosen_channel() == "live", "anything else is live"
    (home / "settings.json").write_text("{half a file")
    assert update.chosen_channel() == "live"


def test_a_test_node_follows_live_until_the_test_channel_is_published(tmp_path, monkeypatch):
    """Set to test before the publisher makes the test channel, a machine stays on live and says why."""
    home = tmp_path / ".dogecoinarcade"
    monkeypatch.setattr(update, "HOME", home)
    _choose(home, "test")
    asked = []
    published = {update.TEST_REVISION_URL: None}
    monkeypatch.setattr(update, "_published_revision", lambda url=update.REVISION_URL: asked.append(url) or published.get(url))
    assert update.following() == "live"
    assert "not published yet" in update.followed_note
    published[update.TEST_REVISION_URL] = "abc1234"
    assert update.following() == "test" and update.followed_note == ""
    assert asked == [update.TEST_REVISION_URL] * 2
    _choose(home, "live")
    assert update.following() == "live" and len(asked) == 2, "a live node never asks for the test channel"


def test_the_test_channel_reads_its_own_branch_revision_and_manifest(monkeypatch):
    seen = []

    class Listed:
        returncode = 0
        stdout = "fedcba9876543210\trefs/heads/test\n"

    monkeypatch.setattr(update, "_find_git", lambda: "git")
    monkeypatch.setattr(update, "_run", lambda *a, **k: seen.append(a) or Listed())
    assert update.remote_revision("test") == "fedcba9"
    assert seen[-1] == ("git", "ls-remote", update.REPO_URL, "refs/heads/test")
    update.remote_revision("live")
    assert seen[-1] == ("git", "ls-remote", update.REPO_URL, "HEAD")

    monkeypatch.setattr(update, "_find_git", lambda: None)
    monkeypatch.setattr(update, "_published_revision", lambda url=update.REVISION_URL: url)
    assert update.remote_revision("test") == update.TEST_REVISION_URL
    assert update.remote_revision("live") == update.REVISION_URL

    urls = []

    def no_manifest(request, timeout=None):
        import urllib.error
        urls.append(request.full_url)
        raise urllib.error.URLError("404")

    monkeypatch.setattr(update.urllib.request, "urlopen", no_manifest)
    with pytest.raises(update.UpdateError, match="source-test.manifest.json"):
        update._signed_manifest("test")
    assert urls == [update.TEST_MANIFEST_URL]


def test_a_git_node_moved_back_to_live_waits_for_live_rather_than_reinstalling(tmp_path, monkeypatch):
    """Ahead of live (it took test's newer release), every check would reinstall and restart, dropping every
    player, for nothing. Git says live's commit is already in its history; it waits."""
    pub = _published(tmp_path, "live")
    mine = tmp_path / "checkout"
    _git("clone", "-q", str(pub), str(mine), cwd=tmp_path)
    live = _git("rev-parse", "--short=7", "HEAD", cwd=mine)
    (mine / "a.txt").write_text("test, not promoted yet")
    _git("commit", "-q", "-am", "test", cwd=mine)
    assert update._ahead_of(mine, live)
    assert not update._ahead_of(mine, "1234567"), "a commit it has never seen is newer"

    monkeypatch.setattr(update, "_layout", lambda: (tmp_path / "venv", mine, True))
    monkeypatch.setattr(update, "following", lambda: "live")
    monkeypatch.setattr(update, "remote_revision", lambda channel=None: live)
    installed, published, available = update.check()
    assert installed != published and not available


def test_an_archive_node_ahead_of_live_waits_too(tmp_path, monkeypatch):
    """No git: the signed release times say it. The refusal of an older release is what tells."""
    checkout = tmp_path / "src"
    checkout.mkdir()
    monkeypatch.setattr(update, "HOME", tmp_path)
    assert not update._ahead_of(checkout, "abc1234"), "nothing installed is recorded: not ahead"
    update._remember({"published": 2000.0})

    def older(channel="live"):
        raise update.UpdateError("the published release is older than the one installed here. That is what a "
                                 "downgrade attack looks like")

    monkeypatch.setattr(update, "_signed_manifest", older)
    assert update._ahead_of(checkout, "abc1234")
    monkeypatch.setattr(update, "_signed_manifest", lambda channel="live": {"published": 3000.0})
    assert not update._ahead_of(checkout, "abc1234")


def test_a_git_node_on_test_fast_forwards_onto_the_test_branch(tmp_path, monkeypatch):
    home = tmp_path / ".dogecoinarcade"
    (home / "venv" / "bin").mkdir(parents=True)
    (home / "src" / ".git").mkdir(parents=True)
    monkeypatch.setattr(update, "HOME", home)
    monkeypatch.setattr(update, "_find_git", lambda: "git")
    monkeypatch.setattr(update, "current_revision", lambda _c: "abc1234")
    monkeypatch.setattr(update, "following", lambda: "test")

    class Done:
        returncode = 0
        stdout = stderr = ""

    ran = []
    monkeypatch.setattr(update, "_run", lambda *a, **k: ran.append(a) or Done())
    monkeypatch.setattr(update.subprocess, "run", lambda argv, **k: Done())
    assert update.update(dry_run=False) == 0
    git = [a[1:] for a in ran if a[0] == "git"]
    assert git[:2] == [("fetch", "--quiet", "origin", "test"), ("merge", "--ff-only", "--quiet", "FETCH_HEAD")]
    assert ("pull", "--ff-only", "--quiet") not in git


def test_an_archive_node_on_test_fetches_the_test_archive_held_to_its_manifest(tmp_path, monkeypatch):
    home = tmp_path / ".dogecoinarcade"
    (home / "venv" / "bin").mkdir(parents=True)
    (home / "src").mkdir(parents=True)
    monkeypatch.setattr(update, "HOME", home)
    monkeypatch.setattr(update, "_find_git", lambda: None)
    monkeypatch.setattr(update, "following", lambda: "test")
    monkeypatch.setattr(update, "_signed_manifest", lambda channel="live": {"channel": channel, "sha256": "ab" * 32})
    called = []
    monkeypatch.setattr(update, "_call_installer", lambda *a: called.append(a) or "abc1234")
    monkeypatch.setattr(update, "_remember", lambda m: None)

    class Done:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(update, "_run", lambda *a, **k: Done())
    monkeypatch.setattr(update.subprocess, "run", lambda argv, **k: Done())
    assert update.update(dry_run=False) == 0
    assert called[0][1:] == ("fetch_source_archive", home / "src", "ab" * 32,
                             update.TEST_ARCHIVE_URL, update.TEST_REVISION_URL)
