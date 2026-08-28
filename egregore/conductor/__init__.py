"""CONDUCTOR — FastAPI media server and feature bus (Architecture §2.8).

Public API:

* :class:`ConductorState` — the integration layer's write side; the
  Conductor app only ever reads it (see ``state.py``'s module docstring).
* :func:`create_app` — builds the FastAPI app from a
  :class:`ConductorState` plus the Lens directory and the resolved party
  password (see ``app.py``'s module docstring for the route table).
"""

from egregore.conductor.app import create_app
from egregore.conductor.state import ConductorState

__all__ = ["create_app", "ConductorState"]
