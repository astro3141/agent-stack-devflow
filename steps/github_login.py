"""devflow's GitHub login — the device flow the panel drives (manifest `login:`, OPERATIONS §44).

The operator presses 연결 under a devflow workflow. The platform runs this under a pseudo-terminal
with DEVFLOW_GITHUB_HOME pointed at this login's own directory; the panel reads the URL and the
code off the output below and shows them. The operator opens the page, types the code there, and
approves. This step polls GitHub until it does, then writes token.json (0600) — which the
platform never opens and only devflow's steps read back (lib/ghauth.py).

Nothing is typed into the panel: the code goes from the panel to GitHub's page, and the token goes
from GitHub to this file.

Which app: DEVFLOW_GITHUB_CLIENT_ID (docker/package.env) — the Client ID of an app the operator
registered once with device flow enabled. A client ID is not a secret. Either kind works:

  GitHub App   permissions: Contents, Issues, Pull requests, Commit statuses (read and write);
               installed on the project's repository only. Turn "Expire user authorization tokens"
               off for a token that does not expire; left on, lib/ghauth.py refreshes it.
  OAuth App    a token with the `repo` scope, over every repository the account can reach.

What a repeat does: "guarded" — a directory that already holds a token is reported connected and
the flow is not started again (reconnecting removes token.json first: see --reconnect).
"""
REPEATABLE = "guarded"
import json, os, sys, time, urllib.error, urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import ghauth  # noqa: E402

DEVICE_URL = "https://github.com/login/device/code"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def whoami(tok):
    req = urllib.request.Request("https://api.github.com/user", headers={
        "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
        "User-Agent": "agent-stack-devflow"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("login", "")


def main(argv):
    home = os.environ.get("DEVFLOW_GITHUB_HOME") or ""
    if not home:
        print("DEVFLOW_GITHUB_HOME is not set: the platform points it at this login's directory")
        return 2
    os.makedirs(home, exist_ok=True)
    path = os.path.join(home, ghauth.TOKEN_FILE)
    if "--reconnect" in argv and os.path.isfile(path):
        os.remove(path)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        print("already logged in")
        return 0
    client_id = os.environ.get("DEVFLOW_GITHUB_CLIENT_ID", "").strip()
    if not client_id:
        print("DEVFLOW_GITHUB_CLIENT_ID is not set: register a GitHub App (or OAuth App) with device "
              "flow enabled, and put its Client ID in docker/package.env (packages/devflow/README.md)")
        return 2
    try:
        dc = ghauth._post_form(DEVICE_URL, {"client_id": client_id, "scope": "repo"})
    except (urllib.error.URLError, OSError) as e:
        print(f"GitHub could not be reached: {type(e).__name__}: {e} "
              "(docker/egress/allow.local needs ^github\\.com$ and ^api\\.github\\.com$)")
        return 1
    if "device_code" not in dc:
        print(f"GitHub refused to start the login: {dc.get('error', '')} {dc.get('error_description', '')}")
        return 1
    # the two lines the panel reads: a URL, and a code in the XXXX-XXXX shape
    print(f"Open {dc['verification_uri']} to authorize devflow")
    print(f"Code to type on that page: {dc['user_code']}", flush=True)
    interval = int(dc.get("interval") or 5)
    deadline = time.time() + int(dc.get("expires_in") or 900)
    while time.time() < deadline:
        time.sleep(interval)
        try:
            got = ghauth._post_form(ghauth.TOKEN_URL, {"client_id": client_id,
                                                       "device_code": dc["device_code"], "grant_type": GRANT})
        except (urllib.error.URLError, OSError):
            continue                               # a lost poll is asked again at the next interval
        err = got.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval = int(got.get("interval") or interval + 5)
            continue
        if err:
            print(f"\nthe login ended: {err} {got.get('error_description', '')}")
            return 1
        doc = ghauth.from_grant(got, client_id)
        try:
            doc["login"] = whoami(doc["access_token"])
        except Exception:
            doc["login"] = ""
        ghauth.write_token(path, doc)
        expiry = ("does not expire" if not doc.get("expires_at")
                  else "is refreshed automatically before it expires")
        print(f"\nsuccessfully logged in as {doc['login'] or '(unknown)'}; the token {expiry}")
        return 0
    print("\nthe code expired before it was approved; press 연결 again")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
