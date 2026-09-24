"""Where a step works and how it reaches out — the stack's answer, never a guess.

Not a step (see remote.py). The stack runs every step with PYTHONPATH pointing at its own modules
(agent-stack docs/packages.md), so `import settings` is the contract for the workspace root and the
egress proxy. These steps used to catch a failed import and carry on with defaults: after the
platform's directory was renamed, every step still "worked" on a fixture while a GitHub call would
have gone out without the proxy — a degraded run that looks like a normal one. A step outside the
stack now says so and stops.
"""
import json, sys


def load(need_proxy=True):
    """(workspace_root, egress environment) from the stack's settings, or a TOOL_FAILURE answer."""
    try:
        import settings
        root = settings.runtime()["paths"]["workspace_root"]
        proxy = settings.egress_env() if need_proxy else None
        return root, proxy
    except Exception as e:
        print(json.dumps({"status": "TOOL_FAILURE",
                          "error": f"the stack's settings are not importable ({type(e).__name__}: {e}); "
                                   "a devflow step runs inside the stack's agent, with the PYTHONPATH "
                                   "it sets (docs/packages.md)"}))
        sys.exit(0)
