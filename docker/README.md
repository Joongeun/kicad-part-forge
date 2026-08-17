# Part-DB, locally — first run

**Status: written against Part-DB's documentation and source (v2.15.0); not yet
executed.** The machine this was developed on has Docker installed but no
running daemon and no `docker compose`, so every command below is transcribed
from Part-DB's install docs rather than from a terminal here. Treat the first
run as the acceptance test — and if a step is wrong, fix it here, because
that's the deliverable.

## What Part-DB is doing in this project

It is the *end* of the pipeline, not the start.

```
parts/*.yaml ─ kifab build ─▶ kifab.kicad_sym + kifab.pretty/     (files on disk)
     │
     └────── kifab sync ────▶ Part-DB REST API   /api/parts        (kifab writes)
                                     │
                                     ▼
                              KiCad HTTP library
                       /en/kicad-api/v1/…  ◀── KiCad reads
```

Part-DB stores **pointers only** — a symbol id string, a footprint name, a
reference prefix and a value. It holds no geometry and generates none. The
`.kicad_sym` and `.kicad_mod` it names must already exist on disk, which is what
`kifab build` is for. If a part shows up in KiCad's chooser with a blank symbol,
the symbol library is missing locally; Part-DB did its job.

## 1. Bring it up

```sh
# Pick a real secret first — it signs session cookies.
sed -i '' "s/CHANGE_ME_openssl_rand_hex_32/$(openssl rand -hex 32)/" docker/compose.yaml

docker compose -f docker/compose.yaml up -d
```

Then create the schema. **This is also what creates the admin user**, and it
prints a generated password exactly once — capture it:

```sh
docker compose -f docker/compose.yaml exec --user=www-data partdb \
    php bin/console doctrine:migrations:migrate
```

`--user=www-data` matters. Running the console as root makes Symfony load the
wrong configuration.

To set the password yourself later:

```sh
docker compose -f docker/compose.yaml exec --user=www-data partdb \
    php bin/console partdb:users:set-password admin
```

Part-DB is now at <http://localhost:8080>. Log in as `admin`.

## 2. Turn API access on — it is off by default

An API token is useless until the *user* is allowed to use the API at all.
Part-DB's permission group `api` has separate operations:

| Operation | What it grants |
|---|---|
| `access_api` — "Access the API" | the token can be used at all |
| `manage_tokens` | the user can create tokens |

Grant both to the admin user (or to a group it belongs to):

**Users → admin → Permissions → API** → set *Access the API* and *Manage API
tokens* to allow. Save.

Without `access_api`, every request comes back `401` — `kifab` recognises that
case and says so, but it cannot fix it for you.

## 3. Create a token

**User Settings → API tokens → Create.** Choose the scope:

| Scope | Enough for |
|---|---|
| Read-Only | `kifab sync --dry-run`, and KiCad's HTTP library |
| **Edit** | **`kifab sync` — this is the one to pick** |
| Admin / Full | not needed |

The token looks like `tcp_XXXXXXXX…` and is shown once. It is a credential:
keep it out of shell history.

```sh
umask 077 && printf %s 'tcp_…' > ~/.config/kifab/partdb-token
export PARTDB_URL=http://localhost:8080
export PARTDB_TOKEN_FILE=~/.config/kifab/partdb-token
```

## 4. Find the right `root_url`

The **User Settings → API tokens** panel also shows an *API endpoints* box with
the exact KiCad `root_url` for your instance. It ends at `/en/kicad-api/` —
**not** `/en/kicad-api/v1/`. KiCad appends the version itself from
`api_version`, so a root with `v1` on it produces `/v1/v1/…` and a library that
lists nothing, with no error.

`kifab httplib` derives it for you and strips a pasted `v1` back off:

```sh
kifab httplib -o ~/Documents/KiCad/9.0/partdb.kicad_httplib
```

That file is mode 0600 because it contains a live token. **Do not commit it**
(the repo's `.gitignore` covers `*.kicad_httplib`). Load it in KiCad:
*Preferences → Manage Symbol Libraries → Add → pick the file*.

If your instance runs in another language, pass `--locale de`; Part-DB routes
the KiCad API under the locale prefix like every other page.

## 5. Register the library

```sh
kifab build parts/ -o build          # the geometry must exist first
kifab sync --dry-run                 # read the plan
kifab sync
```

`kifab sync` is idempotent: run it twice and the second run performs no writes
at all. It identifies parts by **MPN**, so a row somebody already created by
hand is updated rather than duplicated, and it writes only the four EDA fields
(KiCad symbol, KiCad footprint, reference prefix, value). Stock, storage,
prices and suppliers are never touched. If somebody changed one of those four
fields in Part-DB, sync reports a **conflict** and leaves it alone — read the
report, then either fix `parts/`, fix Part-DB, or re-run with `--force`.

The file `partdb-sync.json` records what kifab last wrote. **Commit it.** It is
how sync tells its own previous change apart from somebody else's edit, and how
a second machine reconciles instead of duplicating.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `401 … Access the API` | step 2 was skipped, or the token expired |
| `403` | the token's scope is Read-Only; make an Edit token |
| KiCad's library is empty, no error | `root_url` has `v1` on the end, or no part has EDA metadata yet |
| A part is missing from KiCad | Part-DB only shows parts with EDA metadata on the part, its category, or its footprint. `kifab sync` sets it on the part. |
| `category: This value should not be null` | Part-DB requires a category; `kifab sync` creates one named `kifab` — pass `--category` to use your own |
| Symbol shows as a placeholder in KiCad | the `.kicad_sym` isn't in KiCad's library table locally. Part-DB only sends the *name*. |

Part-DB also ships `php bin/console partdb:kicad:populate --dry-run`, which
guesses `kicad_symbol` / `kicad_footprint` for parts kifab did not create. It is
a different tool with a different contract — it guesses from names, kifab
asserts from the IR — so do not run it over parts kifab manages.
