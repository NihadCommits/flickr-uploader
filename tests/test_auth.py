"""Tests for credentials and login (flickr_uploader.auth).

Never touches the real Credential Manager, a browser or the network: stand-ins
are injected, and conftest.py makes any slip fail.
"""

import getpass
import sys
import warnings
from types import SimpleNamespace

import keyring
import keyring.errors
import pytest
import requests
from flickrapi.auth import FlickrAccessToken
from flickrapi.exceptions import FlickrError
from flickrapi.tokencache import SimpleTokenCache

from flickr_uploader import auth

# Obviously fake values in the right format.
KEY = "a" * 32
SECRET = "b" * 16
CODE = "123-456-789"


class FakeFlickr:
    """Stand-in for flickrapi.FlickrAPI, configured per test via class attributes."""

    instances = []
    valid_key = KEY
    valid_code = CODE
    granted_level = "write"
    login_still_valid = True
    offline = False

    def __init__(self, key, secret, token=None, store_token=True, format="etree"):
        self.key, self.secret, self.store_token, self.format = key, secret, store_token, format
        self.token_cache = SimpleNamespace(token=token)
        FakeFlickr.instances.append(self)

    def get_request_token(self, oauth_callback=None):
        assert oauth_callback == "oob"
        if self.offline:
            raise requests.ConnectionError(
                f"Max retries exceeded with url: /request_token?oauth_consumer_key={self.key}"
            )
        if self.key != self.valid_key:
            raise FlickrError("do_request: Status code 401 received")

    def auth_url(self, perms="read"):
        return f"https://www.flickr.com/services/oauth/authorize?perms={perms}"

    def get_access_token(self, verifier=None):
        if verifier != self.valid_code:
            raise FlickrError("do_request: Status code 401 received")
        self.token_cache.token = FlickrAccessToken(
            "fake-token", "fake-token-secret", self.granted_level, username="NihadTest"
        )

    def token_valid(self, perms="read"):
        if self.offline:
            raise requests.ConnectionError("offline")
        return self.login_still_valid


@pytest.fixture
def fake_flickr(monkeypatch):
    FakeFlickr.instances = []
    monkeypatch.setattr(auth.flickrapi, "FlickrAPI", FakeFlickr)
    for name in ("valid_key", "valid_code", "granted_level", "login_still_valid", "offline"):
        monkeypatch.setattr(FakeFlickr, name, getattr(FakeFlickr, name))
    return FakeFlickr


def token(level="write"):
    return auth.AccessToken("fake-token", "fake-token-secret", level, username="NihadTest")


def configured(store):
    store.save_api_credentials(auth.ApiCredentials(KEY, SECRET))
    store.save_token(token())
    return store


def run_setup(store, capsys, key=KEY, secret=SECRET, code=CODE, read_secret=None):
    secrets = iter([key, secret])
    opened = []
    exit_code = auth.main(
        ["setup"],
        store=store,
        read_secret=read_secret or (lambda prompt: next(secrets)),
        read_line=lambda prompt: code,
        open_browser=opened.append,
    )
    out, err = capsys.readouterr()
    return exit_code, out + err, opened


# --- Credential objects -------------------------------------------------------


def test_api_credentials_forgive_blanks_and_upper_case():
    credentials = auth.ApiCredentials.parse(f"  {KEY.upper()}\n", f" {SECRET} ")
    assert (credentials.key, credentials.secret) == (KEY, SECRET)


@pytest.mark.parametrize(
    ("key", "secret"),
    [
        (KEY[:-1], SECRET),  # too short
        (SECRET, KEY),  # swapped
        ("x" * 32, SECRET),  # not hexadecimal
        (KEY, ""),
    ],
)
def test_malformed_api_credentials_are_refused_without_echoing_them(key, secret):
    with pytest.raises(auth.InvalidCredentialsError) as error:
        auth.ApiCredentials.parse(key, secret)
    message = str(error.value)
    assert not (key and key in message)
    assert not (secret and secret in message)


def test_secrets_never_show_in_repr():
    credentials = auth.ApiCredentials(KEY, SECRET)
    access = token()
    for text in (repr(credentials), str(credentials), repr(access), str(access)):
        assert SECRET not in text
        assert KEY not in text
        assert "fake-token" not in text
    assert "NihadTest" in repr(access)


def test_access_token_levels():
    assert token("delete").has_level("write")
    assert token("write").has_level("write")
    assert not token("read").has_level("write")
    with pytest.raises(auth.InvalidCredentialsError):
        token("admin")
    with pytest.raises(auth.InvalidCredentialsError):
        auth.AccessToken("", "secret", "write")


# --- Credential store ---------------------------------------------------------


def test_store_round_trip(store, backend):
    assert store.load_api_credentials() is None
    assert store.load_token() is None
    configured(store)
    assert store.load_api_credentials() == auth.ApiCredentials(KEY, SECRET)
    assert store.load_token() == token()
    assert set(backend.entries) == {
        ("flickr-uploader", "api-credentials"),
        ("flickr-uploader", "access-token"),
    }


def test_new_api_credentials_drop_the_old_token(store):
    configured(store)
    store.save_api_credentials(auth.ApiCredentials("c" * 32, "d" * 16))
    assert store.load_token() is None


def test_clear(store):
    store.clear()  # nothing stored: fine
    configured(store).clear()
    assert store.load_api_credentials() is None
    assert store.load_token() is None


def test_damaged_entry_is_reported_without_its_content(store, backend):
    backend.entries[("flickr-uploader", "api-credentials")] = '{"key": "' + KEY + '"'
    with pytest.raises(auth.CredentialStoreError) as error:
        store.load_api_credentials()
    assert KEY not in str(error.value)


def test_backend_failures_are_wrapped(store, backend):
    def broken(*args):
        raise keyring.errors.KeyringError(f"vault locked, value was {SECRET}")

    backend.set_password = broken
    with pytest.raises(auth.CredentialStoreError) as error:
        store.save_api_credentials(auth.ApiCredentials(KEY, SECRET))
    assert SECRET not in str(error.value)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager")
def test_default_backend_is_always_the_windows_credential_manager(monkeypatch, backend):
    from keyring.backends.Windows import WinVaultKeyring

    # keyring's auto-detection would take e.g. a plaintext file backend if
    # one is installed and preferred; the choice must not depend on that.
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    assert type(auth._default_backend()) is WinVaultKeyring


# --- Logged-in client ---------------------------------------------------------


def test_flickr_api_needs_setup(store):
    with pytest.raises(auth.NotConfiguredError):
        auth.flickr_api(store)


def test_flickr_api_uses_stored_login_and_never_the_disk_cache(store, fake_flickr):
    flickr = auth.flickr_api(configured(store))
    assert (flickr.key, flickr.secret, flickr.store_token) == (KEY, SECRET, False)
    assert flickr.token_cache.token.token == "fake-token"


def test_real_flickrapi_writes_nothing_to_the_home_folder(store, tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    flickr = auth.flickr_api(configured(store))  # the real flickrapi class
    assert isinstance(flickr.token_cache, SimpleTokenCache)
    assert not (tmp_path / ".flickr").exists()


def test_check_login(store, fake_flickr):
    configured(store)
    assert auth.check_login(store) == token()
    fake_flickr.login_still_valid = False
    with pytest.raises(auth.InvalidCredentialsError):
        auth.check_login(store)


# --- Login flow ---------------------------------------------------------------


def test_login_flow(fake_flickr):
    flow = auth.LoginFlow(auth.ApiCredentials(KEY, SECRET))
    assert flow.url.endswith("perms=write")
    assert flow.finish(f" {CODE}\n") == token()


def test_login_flow_with_rejected_key(fake_flickr):
    with pytest.raises(auth.InvalidCredentialsError, match="API key or secret"):
        auth.LoginFlow(auth.ApiCredentials("c" * 32, SECRET))


def test_login_flow_with_wrong_code(fake_flickr):
    flow = auth.LoginFlow(auth.ApiCredentials(KEY, SECRET))
    with pytest.raises(auth.InvalidCredentialsError, match="code"):
        flow.finish("999-999-999")


def test_login_flow_with_too_few_rights(fake_flickr):
    fake_flickr.granted_level = "read"
    flow = auth.LoginFlow(auth.ApiCredentials(KEY, SECRET))
    with pytest.raises(auth.InvalidCredentialsError, match="write"):
        flow.finish(CODE)


# --- Command line -------------------------------------------------------------


def test_setup(store, fake_flickr, capsys):
    exit_code, output, opened = run_setup(store, capsys, key=f" {KEY.upper()} ")

    assert exit_code == 0
    assert store.load_api_credentials() == auth.ApiCredentials(KEY, SECRET)
    assert store.load_token() == token()
    assert opened == ["https://www.flickr.com/services/oauth/authorize?perms=write"]
    assert "NihadTest" in output
    assert KEY not in output and SECRET not in output


def test_failed_setup_leaves_the_old_login_untouched(store, fake_flickr, capsys):
    configured(store)
    exit_code, output, _ = run_setup(store, capsys, key="c" * 32, secret="d" * 16, code="bad")
    assert exit_code == 1
    assert store.load_api_credentials() == auth.ApiCredentials(KEY, SECRET)
    assert store.load_token() == token()


def test_setup_with_wrong_code_stores_nothing(store, fake_flickr, capsys):
    exit_code, output, _ = run_setup(store, capsys, code="999-999-999")
    assert exit_code == 1
    assert store.load_api_credentials() is None
    assert "Code" in output or "code" in output


def test_setup_with_malformed_key_does_not_echo_it(store, fake_flickr, capsys):
    exit_code, output, opened = run_setup(store, capsys, key="not-a-key-" + SECRET)
    assert exit_code == 1
    assert SECRET not in output
    assert opened == []


def test_setup_refuses_a_terminal_that_shows_the_input(store, fake_flickr, capsys):
    def visible_input(prompt):
        warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning, stacklevel=2)
        return KEY

    exit_code, output, _ = run_setup(store, capsys, read_secret=visible_input)
    assert exit_code == 1
    assert "PowerShell" in output
    assert store.load_api_credentials() is None


def test_setup_offline_does_not_leak_the_key(store, fake_flickr, capsys):
    fake_flickr.offline = True
    exit_code, output, _ = run_setup(store, capsys)
    assert exit_code == 1
    assert "nicht erreichbar" in output
    assert KEY not in output


def test_status(store, fake_flickr, capsys):
    assert auth.main(["status"], store=store) == 1
    assert "Nicht eingerichtet" in capsys.readouterr().out

    configured(store)
    assert auth.main(["status"], store=store) == 0
    assert "NihadTest" in capsys.readouterr().out

    fake_flickr.offline = True
    assert auth.main(["status"], store=store) == 1
    assert "nicht erreichbar" in capsys.readouterr().out

    fake_flickr.offline = False
    fake_flickr.login_still_valid = False
    assert auth.main(["status"], store=store) == 1
    assert "setup" in capsys.readouterr().err


def test_logout(store, capsys):
    assert auth.main(["logout"], store=configured(store)) == 0
    assert store.load_api_credentials() is None
    assert auth.AUTHORIZED_APPS_URL in capsys.readouterr().out


def test_usage(store, capsys):
    assert auth.main([], store=store) == 0
    assert auth.main(["frobnicate"], store=store) == 2


# --- Leftovers of the old helper scripts --------------------------------------


def test_plaintext_leftovers_are_reported_without_values(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert auth.plaintext_leftovers({}) == []

    (tmp_path / ".flickr").mkdir()
    found = auth.plaintext_leftovers({"FLICKR_API_KEY": KEY, "FLICKR_API_SECRET": SECRET})
    assert len(found) == 3
    assert not any(KEY in item or SECRET in item for item in found)
