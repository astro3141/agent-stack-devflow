"""Where devflow's GitHub credential comes from. A library for the steps (steps/ holds only steps).

Two sources, in this order:

  1. the package's own login (manifest `login:`, OPERATIONS §44): the operator presses 연결 on the
     panel, GitHub shows a page, the operator types the code the panel shows. steps/github_login.py
     runs that device flow and writes token.json into the login's directory, which the platform
     never opens. Only this library reads it back.
  2. DEVFLOW_GITHUB_TOKEN in docker/package.env — a personal access token the operator holds.

A token from (1) may carry an expiry: a GitHub App that did not opt out of expiring user tokens
issues an 8-hour token with a 6-month refresh token. It is refreshed here, before it expires, and
written back — so nobody logs in again as long as devflow runs at least once in six months. An
OAuth App's token, or a GitHub App's with expiry turned off, has none and is used as it is.

The refresh is serialised with a file lock: two steps refreshing at once would each spend the same
refresh token, and GitHub accepts it once.
"""
import fcntl, json, os, time, urllib.error, urllib.parse, urllib.request

LOGIN_NAME = "pkg-devflow"            # login_helper's name for the `pkg:devflow` login
TOKEN_FILE = "token.json"
TOKEN_URL = "https://github.com/login/oauth/access_token"
EARLY_S = 600                         # refresh this long before the token would expire


class AuthError(Exception):
    def __init__(self, msg, hint=""):
        super().__init__(msg)
        self.hint = hint


def login_dir():
    """This login's directory: DEVFLOW_GITHUB_HOME while the login flow runs (the platform points
    it there), <logins_root>/pkg-devflow for every step afterwards."""
    if os.environ.get("DEVFLOW_GITHUB_HOME"):
        return os.environ["DEVFLOW_GITHUB_HOME"]
    root = os.environ.get("DEVFLOW_LOGINS_ROOT")
    if not root:
        try:
            import settings  # the platform's, on PYTHONPATH (stackenv)
            root = settings.runtime()["paths"]["logins_root"]
        except Exception:
            root = "/route"
    return os.path.join(root, LOGIN_NAME)


def _post_form(url, fields, opener=None):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Accept": "application/json", "User-Agent": "agent-stack-devflow"})
    with (opener or urllib.request.build_opener()).open(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def write_token(path, doc):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(tmp, path)


def from_grant(got, client_id, now=None):
    """The document kept for a token GitHub just issued (device flow or refresh)."""
    now = now or time.time()
    doc = {"access_token": got["access_token"], "token_type": got.get("token_type", "bearer"),
           "scope": got.get("scope", ""), "client_id": client_id, "obtained_at": now}
    if got.get("expires_in"):
        doc["expires_at"] = now + int(got["expires_in"])
    if got.get("refresh_token"):
        doc["refresh_token"] = got["refresh_token"]
        if got.get("refresh_token_expires_in"):
            doc["refresh_expires_at"] = now + int(got["refresh_token_expires_in"])
    return doc


def through_app():
    """Whether the token devflow uses is the App login's (a user-to-server token: what it writes is
    marked as performed via the App) rather than an operator's personal token."""
    return os.path.isfile(os.path.join(login_dir(), TOKEN_FILE))


def token(proxy_env=None, now=None, post=None):
    """A usable token, or AuthError naming what the operator can do."""
    now = now or time.time()
    path = os.path.join(login_dir(), TOKEN_FILE)
    if os.path.isfile(path):
        return _login_token(path, proxy_env, now, post)
    if os.environ.get("DEVFLOW_GITHUB_TOKEN"):
        return os.environ["DEVFLOW_GITHUB_TOKEN"]
    raise AuthError("no GitHub credential",
                    hint="press 연결 under the devflow workflow on the panel (GitHub login), or put "
                         "DEVFLOW_GITHUB_TOKEN=<token> in docker/package.env; either way "
                         "^api\\.github\\.com$ and ^github\\.com$ must be in docker/egress/allow.local")


def _login_token(path, proxy_env, now, post):
    doc = json.load(open(path, encoding="utf-8"))
    if not doc.get("expires_at") or now < doc["expires_at"] - EARLY_S:
        return doc["access_token"]
    if not doc.get("refresh_token"):
        raise AuthError("the GitHub login has expired and carries no refresh token",
                        hint="press 연결 under the devflow workflow on the panel again")
    with open(path + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        doc = json.load(open(path, encoding="utf-8"))          # someone may have refreshed meanwhile
        if now < doc.get("expires_at", 0) - EARLY_S:
            return doc["access_token"]
        if doc.get("refresh_expires_at") and now >= doc["refresh_expires_at"]:
            raise AuthError("the GitHub login's refresh token has expired (unused for six months)",
                            hint="press 연결 under the devflow workflow on the panel again")
        opener = None
        if proxy_env and post is None:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(
                {"https": proxy_env.get("HTTPS_PROXY"), "http": proxy_env.get("HTTP_PROXY")}))
        fields = {"client_id": doc["client_id"], "grant_type": "refresh_token",
                  "refresh_token": doc["refresh_token"]}
        try:
            got = post(TOKEN_URL, fields) if post else _post_form(TOKEN_URL, fields, opener)
        except (urllib.error.URLError, OSError) as e:
            raise AuthError(f"refreshing the GitHub login failed: {type(e).__name__}: {e}")
        if "access_token" not in got:
            raise AuthError(f"GitHub refused the refresh: {got.get('error', 'unknown')}",
                            hint="press 연결 under the devflow workflow on the panel again")
        new = from_grant(got, doc["client_id"], now)
        new["login"] = doc.get("login", "")
        write_token(path, new)
        return new["access_token"]
