"""web-terminal — dashboard backend plugin only.

The agent/gateway side has nothing to register: the plugin's whole surface is
``dashboard/plugin_api.py``, which ``hermes dashboard`` mounts under
``/api/plugins/web-terminal/`` once the plugin is listed in ``plugins.enabled``.
This module (plus ``plugin.yaml``) exists so ``hermes plugins install`` and
``hermes plugins enable web-terminal`` can resolve the name through the normal
plugin registry; a gateway that loads it gets no hooks, tools, or prompt text.
"""


def register(ctx) -> None:
    """Nothing to register on the agent side."""
    return None
