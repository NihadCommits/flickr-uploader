"""Fixtures shared by all test modules."""

import socket

import keyring.errors
import pytest

from flickr_uploader import auth, db


class MemoryKeyring:
    """In-memory stand-in for the Windows Credential Manager."""

    def __init__(self):
        self.entries = {}

    def get_password(self, service, username):
        return self.entries.get((service, username))

    def set_password(self, service, username, password):
        self.entries[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.entries:
            raise keyring.errors.PasswordDeleteError("not found")
        del self.entries[(service, username)]


@pytest.fixture
def backend():
    """An empty in-memory credential backend."""
    return MemoryKeyring()


@pytest.fixture
def store(backend):
    """A credential store backed by memory, never by the real Credential Manager."""
    return auth.CredentialStore(backend)


@pytest.fixture(autouse=True)
def _no_real_credentials_and_no_network(monkeypatch):
    """No test may touch the real Windows Credential Manager or the network.

    Tests inject in-memory stand-ins; anything that slips through fails loudly
    instead of reading or overwriting the user's real Flickr login.
    """

    def refuse_credentials(*args, **kwargs):
        raise AssertionError("test tried to use the real Windows Credential Manager")

    try:
        from keyring.backends.Windows import WinVaultKeyring
    except ImportError:  # pragma: no cover - not on Windows
        pass
    else:
        for method in ("get_password", "set_password", "delete_password"):
            monkeypatch.setattr(WinVaultKeyring, method, refuse_credentials)

    def refuse_network(self, address):
        raise AssertionError(f"test tried to connect to {address}")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)


@pytest.fixture
def database(tmp_path):
    """An opened database in a temporary folder."""
    database = db.Database(tmp_path / "db" / "state.db")
    database.open()
    yield database
    database.close()


@pytest.fixture
def conn(database):
    """A connection to the temporary database."""
    with database.connect() as conn:
        yield conn
