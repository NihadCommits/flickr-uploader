r"""Flickr credentials: storage in the Windows Credential Manager and OAuth login.

Nothing secret ever touches a file:

- API key/secret and the OAuth access token live only in the Windows
  Credential Manager (keyring's WinVaultKeyring). There is deliberately no
  plaintext fallback: on Windows the Credential Manager is always available,
  and a file would be the weakest spot.
- flickrapi is always created with store_token=False; otherwise it silently
  writes the token in plaintext to %USERPROFILE%\.flickr\oauth-tokens.sqlite.
- repr() of credential objects never shows secrets, so they can't leak into
  logs, tracebacks or bug reports.

Set up in the settings tab of the app (gui/api.py uses LoginFlow and
CredentialStore from here) or from a terminal; either way the input is hidden
and goes straight into the Credential Manager. Never paste credentials
anywhere else:

    .venv\Scripts\python.exe -m flickr_uploader.auth setup
"""

import getpass
import json
import os
import re
import sys
import warnings
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import flickrapi
from flickrapi.auth import FlickrAccessToken
from flickrapi.exceptions import FlickrError

SERVICE = "flickr-uploader"
_API_ENTRY = "api-credentials"
_TOKEN_ENTRY = "access-token"
# Upload, create albums, add tags. Never "delete": the tool must not be able to.
REQUIRED_PERMS = "write"
_ACCESS_LEVELS = ("read", "write", "delete")
_API_KEY_PATTERN = re.compile(r"[0-9a-f]{32}")
_API_SECRET_PATTERN = re.compile(r"[0-9a-f]{16}")
API_KEY_URL = "https://www.flickr.com/services/apps/create/"
# Where the user can revoke the access this tool was granted.
AUTHORIZED_APPS_URL = "https://www.flickr.com/services/auth/list.gne"


class AuthError(Exception):
    """Base class for errors raised by this module. Messages never contain secrets."""


class NotConfiguredError(AuthError):
    """No API credentials or no login stored yet."""


class InvalidCredentialsError(AuthError):
    """Malformed input, or Flickr rejected the key, secret, code or login."""


class CredentialStoreError(AuthError):
    """The Windows Credential Manager could not be used."""


class KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


@dataclass(frozen=True)
class ApiCredentials:
    """API key and secret of the Flickr app registered by the user."""

    key: str
    secret: str

    def __post_init__(self) -> None:
        # The values are never part of the message.
        if not _API_KEY_PATTERN.fullmatch(self.key):
            raise InvalidCredentialsError("the API key must be 32 hexadecimal characters")
        if not _API_SECRET_PATTERN.fullmatch(self.secret):
            raise InvalidCredentialsError("the API secret must be 16 hexadecimal characters")

    @classmethod
    def parse(cls, key: str, secret: str) -> "ApiCredentials":
        """Build from user input, forgiving surrounding blanks and upper case."""
        return cls(key.strip().lower(), secret.strip().lower())

    def __repr__(self) -> str:
        return f"ApiCredentials(key='{self.key[:4]}…', secret=***)"


@dataclass(frozen=True)
class AccessToken:
    """OAuth access token granted by the user, plus who granted it."""

    token: str
    token_secret: str
    access_level: str
    username: str = ""
    fullname: str = ""
    user_nsid: str = ""

    def __post_init__(self) -> None:
        if not self.token or not self.token_secret:
            raise InvalidCredentialsError("the access token is incomplete")
        if self.access_level not in _ACCESS_LEVELS:
            raise InvalidCredentialsError("the access token has an unknown access level")

    def has_level(self, level: str) -> bool:
        return _ACCESS_LEVELS.index(self.access_level) >= _ACCESS_LEVELS.index(level)

    def to_flickrapi(self) -> FlickrAccessToken:
        return FlickrAccessToken(
            self.token,
            self.token_secret,
            self.access_level,
            fullname=self.fullname,
            username=self.username,
            user_nsid=self.user_nsid,
        )

    @classmethod
    def from_flickrapi(cls, token: FlickrAccessToken) -> "AccessToken":
        return cls(
            token=token.token,
            token_secret=token.token_secret,
            access_level=token.access_level,
            username=token.username,
            fullname=token.fullname,
            user_nsid=token.user_nsid,
        )

    def __repr__(self) -> str:
        return (
            f"AccessToken(username={self.username!r}, access_level={self.access_level!r}, "
            "token=***)"
        )


class CredentialStore:
    """API credentials and access token in the Windows Credential Manager.

    Stored as two generic credentials, "flickr-uploader/api-credentials" and
    "flickr-uploader/access-token", visible in the Windows "Credential
    Manager" control panel.
    """

    def __init__(self, backend: KeyringBackend | None = None, service: str = SERVICE) -> None:
        self._backend = backend if backend is not None else _default_backend()
        self.service = service

    def load_api_credentials(self) -> ApiCredentials | None:
        data = self._load(_API_ENTRY)
        return None if data is None else self._build(ApiCredentials, data)

    def save_api_credentials(self, credentials: ApiCredentials) -> None:
        # A token belongs to the API key it was issued for.
        self._delete(_TOKEN_ENTRY)
        self._save(_API_ENTRY, {"key": credentials.key, "secret": credentials.secret})

    def load_token(self) -> AccessToken | None:
        data = self._load(_TOKEN_ENTRY)
        return None if data is None else self._build(AccessToken, data)

    def save_token(self, token: AccessToken) -> None:
        self._save(
            _TOKEN_ENTRY,
            {
                "token": token.token,
                "token_secret": token.token_secret,
                "access_level": token.access_level,
                "username": token.username,
                "fullname": token.fullname,
                "user_nsid": token.user_nsid,
            },
        )

    def clear(self) -> None:
        self._delete(_TOKEN_ENTRY)
        self._delete(_API_ENTRY)

    def _load(self, entry: str) -> dict | None:
        try:
            raw = self._backend.get_password(self.service, entry)
        except Exception as error:
            raise CredentialStoreError(f"could not read {entry}: {type(error).__name__}") from None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            raise CredentialStoreError(f"{entry} is damaged; run setup again") from None
        if not isinstance(data, dict):
            raise CredentialStoreError(f"{entry} is damaged; run setup again")
        return data

    def _build(self, cls: type, data: dict):
        try:
            return cls(**data)
        except (TypeError, InvalidCredentialsError):
            raise CredentialStoreError(
                f"stored {cls.__name__} is damaged; run setup again"
            ) from None

    def _save(self, entry: str, data: dict) -> None:
        try:
            self._backend.set_password(self.service, entry, json.dumps(data))
        except Exception as error:
            raise CredentialStoreError(f"could not store {entry}: {type(error).__name__}") from None

    def _delete(self, entry: str) -> None:
        try:
            if self._backend.get_password(self.service, entry) is not None:
                self._backend.delete_password(self.service, entry)
        except Exception as error:
            raise CredentialStoreError(
                f"could not delete {entry}: {type(error).__name__}"
            ) from None


def _default_backend() -> KeyringBackend:
    if sys.platform == "win32":
        # Picked explicitly instead of keyring's auto-detection, which would
        # also accept a plaintext backend if one happens to be installed.
        from keyring.backends.Windows import WinVaultKeyring

        return WinVaultKeyring()
    raise CredentialStoreError("credentials can only be stored on Windows")


# --- Talking to Flickr --------------------------------------------------------


def _new_flickr(
    credentials: ApiCredentials, token: AccessToken | None = None, *, format: str = "parsed-json"
) -> flickrapi.FlickrAPI:
    return flickrapi.FlickrAPI(
        credentials.key,
        credentials.secret,
        token=token.to_flickrapi() if token else None,
        store_token=False,  # never let flickrapi write the token to disk
        format=format,
    )


def flickr_api(
    store: CredentialStore | None = None, *, format: str = "parsed-json"
) -> flickrapi.FlickrAPI:
    """Return a flickrapi client that is logged in with the stored credentials."""
    store = store if store is not None else CredentialStore()
    credentials, token = store.load_api_credentials(), store.load_token()
    if credentials is None or token is None:
        raise NotConfiguredError("not set up yet: run  python -m flickr_uploader.auth setup")
    return _new_flickr(credentials, token, format=format)


def check_login(store: CredentialStore | None = None) -> AccessToken:
    """Ask Flickr whether the stored login is still valid and return it.

    Raises InvalidCredentialsError if Flickr rejects it (e.g. access revoked),
    and lets network errors through: "offline" is not "logged out".
    """
    store = store if store is not None else CredentialStore()
    flickr = flickr_api(store)
    if not flickr.token_valid(perms=REQUIRED_PERMS):
        raise InvalidCredentialsError("Flickr no longer accepts the stored login; run setup again")
    return store.load_token()


class LoginFlow:
    """OAuth login without a local web server ("out of band").

    The user opens `url`, allows access on Flickr and copies the code Flickr
    shows back into finish(). The code is single-use and expires quickly.
    """

    def __init__(self, credentials: ApiCredentials, perms: str = REQUIRED_PERMS) -> None:
        self.perms = perms
        self._flickr = _new_flickr(credentials)
        try:
            self._flickr.get_request_token(oauth_callback="oob")
        except FlickrError:
            raise InvalidCredentialsError(
                "Flickr rejected the API key or secret; please check both"
            ) from None
        self.url = self._flickr.auth_url(perms=perms)

    def finish(self, code: str) -> AccessToken:
        try:
            self._flickr.get_access_token(code.strip())
        except FlickrError:
            raise InvalidCredentialsError(
                "Flickr did not accept the code; it may have expired, please start again"
            ) from None
        token = AccessToken.from_flickrapi(self._flickr.token_cache.token)
        if not token.has_level(self.perms):
            raise InvalidCredentialsError(
                f"access was granted for {token.access_level!r}, but {self.perms!r} is needed"
            )
        return token


def plaintext_leftovers(environ: Mapping[str, str] | None = None) -> list[str]:
    """Find credentials that older versions of the helper scripts left behind.

    Only reports; deleting is left to the user.
    """
    env = os.environ if environ is None else environ
    leftovers = []
    cache = Path(os.path.expanduser("~")) / ".flickr"
    if cache.exists():
        leftovers.append(f"flickrapi token cache (plaintext): {cache}")
    for name in ("FLICKR_API_KEY", "FLICKR_API_SECRET"):
        if env.get(name):
            leftovers.append(f"environment variable {name} (plaintext in the registry)")
    return leftovers


# --- Command line -------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    store: CredentialStore | None = None,
    read_secret: Callable[[str], str] = getpass.getpass,
    read_line: Callable[[str], str] = input,
    open_browser: Callable[[str], object] = webbrowser.open,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "help"
    try:
        store = store if store is not None else CredentialStore()
        if command == "setup":
            with warnings.catch_warnings():
                # getpass falls back to *visible* input on terminals that can't
                # hide it (e.g. Git Bash) and only warns. Refuse instead.
                warnings.simplefilter("error", getpass.GetPassWarning)
                return _setup(store, read_secret, read_line, open_browser)
        if command == "status":
            return _status(store)
        if command == "logout":
            store.clear()
            print("Zugangsdaten aus dem Windows-Anmeldeinformationsspeicher entfernt.")
            print(f"Zugriff der App auf Flickr hier widerrufen: {AUTHORIZED_APPS_URL}")
            return 0
    except AuthError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return 1
    except getpass.GetPassWarning:
        print(
            "Fehler: Dieses Terminal kann die Eingabe nicht verbergen. "
            "Bitte in PowerShell oder der Windows-Eingabeaufforderung ausführen.",
            file=sys.stderr,
        )
        return 1
    except OSError as error:
        # Connection errors carry the request URL, which contains the API key:
        # only the error type is shown.
        print(f"Fehler: Flickr nicht erreichbar ({type(error).__name__}).", file=sys.stderr)
        return 1
    print("Verwendung: python -m flickr_uploader.auth setup | status | logout")
    return 0 if command == "help" else 2


def _setup(store, read_secret, read_line, open_browser) -> int:
    print("Flickr-Uploader einrichten")
    if store.load_api_credentials() is not None:
        print("(Bereits eingerichtet, die gespeicherten Zugangsdaten werden ersetzt.)")
    print(f"API-Key und Secret findest du unter {API_KEY_URL} (bzw. bei deiner App).")
    print("Die Eingabe wird nicht angezeigt und landet direkt im")
    print("Windows-Anmeldeinformationsspeicher, nie in einer Datei.\n")
    credentials = ApiCredentials.parse(read_secret("API-Key: "), read_secret("API-Secret: "))

    flow = LoginFlow(credentials)
    print("\nIm Browser öffnet sich jetzt Flickr. Dort den Zugriff erlauben")
    print('("OK, I\'ll authorize it") und den angezeigten Code hier eingeben.')
    print(f"Falls sich kein Browser öffnet: {flow.url}\n")
    open_browser(flow.url)
    token = flow.finish(read_line("Code von Flickr: "))

    # Stored only now: a half-finished setup must not leave anything behind.
    store.save_api_credentials(credentials)
    store.save_token(token)
    print(f"\nFertig. Angemeldet als {token.username} (Rechte: {token.access_level}).")
    _warn_leftovers()
    return 0


def _status(store) -> int:
    if store.load_api_credentials() is None or store.load_token() is None:
        print("Nicht eingerichtet: python -m flickr_uploader.auth setup")
        return 1
    try:
        token = check_login(store)
    except OSError as error:  # requests' connection errors are OSErrors
        print(f"Zugangsdaten gespeichert, Flickr aber nicht erreichbar ({type(error).__name__}).")
        return 1
    print(f"Angemeldet als {token.username} (Rechte: {token.access_level}).")
    _warn_leftovers()
    return 0


def _warn_leftovers() -> None:
    leftovers = plaintext_leftovers()
    if leftovers:
        print("\nHinweis: Alte Zugangsdaten im Klartext gefunden, bitte entfernen:")
        for leftover in leftovers:
            print(f"  - {leftover}")


if __name__ == "__main__":
    sys.exit(main())
