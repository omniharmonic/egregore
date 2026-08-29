

import os as _os

# Tests must not depend on whatever LLM server happens to be running on the
# developer's machine: the default brain under test is the heuristic.
_os.environ.setdefault("EGREGORE_LLM_AUTODETECT", "0")
