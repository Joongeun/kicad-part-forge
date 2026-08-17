"""Part-DB — where a finished part gets registered so KiCad can find it.

**Direction of travel matters here and is easy to get backwards.** Part-DB is an
*inventory* database. Its KiCad integration is a **read-only HTTP library**: it
serves KiCad a symbol id string, a footprint name and some fields. It never
holds geometry, and the `.kicad_sym` / `.kicad_mod` it names must already exist
on disk. So Part-DB sits at the *end* of this pipeline, not the start:

```
    parts/*.yaml ──▶ kifab build ──▶ .kicad_sym + .pretty/   (on disk)
          │
          └──▶ kifab sync ──▶ Part-DB REST API  (/api/parts — we write)
                                    │
                                    ▼
                          KiCad HTTP library API
                     ({root}/en/kicad-api/v1 — KiCad reads)
```

Two API surfaces, deliberately in two modules, because confusing them is the
mistake this package exists to prevent:

* `client.py` — the **Part-DB REST API**. Token auth, JSON-LD, `/api/parts`.
  This is how *we write*.
* `httplib.py` — generates the `.kicad_httplib` file that points KiCad at the
  **KiCad HTTP library API**. This is how *KiCad reads*. Nothing in kifab ever
  calls that endpoint; only KiCad does.

`sync.py` is the reconciliation between `parts/` and the inventory. It is
idempotent by construction: it computes a plan, and a plan with nothing in it
performs no requests.
"""

from .client import (
    PartDbClient,
    PartDbError,
    PartDbHttpError,
    RemotePart,
    Transport,
    UrlTransport,
)
from .httplib import (
    DEFAULT_API_VERSION,
    HttpLibrarySource,
    httplib_document,
    kicad_api_root,
    write_httplib,
)
from .sync import (
    MANAGED_FIELDS,
    Action,
    DesiredPart,
    SyncPlan,
    SyncState,
    SyncStep,
    apply_plan,
    desired_from_part,
    plan_sync,
)

__all__ = [
    "DEFAULT_API_VERSION",
    "MANAGED_FIELDS",
    "Action",
    "DesiredPart",
    "HttpLibrarySource",
    "PartDbClient",
    "PartDbError",
    "PartDbHttpError",
    "RemotePart",
    "SyncPlan",
    "SyncState",
    "SyncStep",
    "Transport",
    "UrlTransport",
    "apply_plan",
    "desired_from_part",
    "httplib_document",
    "kicad_api_root",
    "plan_sync",
    "write_httplib",
]
