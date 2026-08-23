# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repository Is

A **data-only** repository: a small [ColDP](https://github.com/CatalogueOfLife/coldp) dataset listing ~123 organisms named after famous people. There is no build, no test suite, no CI, and no application code — only the data files, a one-line curl helper, and a git hook.

Published as:
- ChecklistBank dataset **37354** — https://www.checklistbank.org/dataset/37354/
- GBIF dataset `00e791be-36ae-40ee-8165-0b2cb0b8c84f`

## Publishing Pipeline

**Pushing to `master` is the release.** ChecklistBank's settings for dataset 37354 are:

```
data access : https://github.com/mdoering/famous-organism/archive/master.zip
data format : coldp
gbif sync lock : true
```

CLB re-downloads that zip on import (manually triggered from the CLB UI or scheduled); GBIF is then synced from CLB.

Check what CLB actually ingested:

```sh
curl -s https://api.checklistbank.org/dataset/37354 | jq
curl -s "https://api.checklistbank.org/dataset/37354/import?limit=1" | jq   # counts + import issues
curl -s https://api.checklistbank.org/dataset/37354/settings | jq
```

The import report is the closest thing this repo has to a test run — check `issuesCount`, `nameCount`, `referenceCount`, `vernacularCount` and `mediaCount` after a change lands.

## Files

| File | Role |
|---|---|
| `name_usage.tsv` | The core data. ColDP **NameUsage** entity (the flattened Name+Taxon+Synonym alternative). |
| `vernacular_name.tsv` | ColDP **VernacularName** entity. `taxonID` → `name_usage.ID`. |
| `media.tsv` | ColDP **Media** entity. `taxonID` → `name_usage.ID`. |
| `reference.bib` | ColDP BibTeX reference file. Entry keys are the primary keys referenced by `nameReferenceID`. |
| `metadata.yaml` | ColDP dataset metadata. Its `$schema=metadata.json` header points at the schema in the `coldp` repo, not at a local file. |
| `lookup.sh` | Reads DOIs on stdin, appends CrossRef BibTeX to `newrefs.bib` (gitignored scratch file). |
| `load.py` | Bulk loader for external sources. Parses the six English Wikipedia "List of organisms named after famous people" pages, resolves classification via ChecklistBank and references via CrossRef, and emits new ColDP rows. |
| `pre-commit.hook` | Repo copy of the git hook that stamps today's date into `metadata.yaml` `issued:`/`version:`. |

All TSVs: tab-delimited, header row, **LF** line endings, UTF-8, no quoting (no field may contain a tab).

The file names use snake_case; ChecklistBank resolves them by stripping `-`/`_`/space and lowercasing (`ColdpTerm.normalize`), so `vernacular_name.tsv` matches the `VernacularName` entity.

## Data Model

`name_usage.tsv` columns (0-based, in file order):

```
0  ID              5  etymology             10 extinct       15 family
1  parentID        6  scientificName        11 kingdom       16 link
2  basionymID      7  authorship            12 phylum
3  status          8  namePublishedInYear   13 class
4  rank            9  nameReferenceID       14 order
```

- **IDs** are plain integers, currently 1–124 with gaps; a new record takes `max(ID)+1`.
- **`parentID` is only used by synonyms** (3 rows), where it points at the accepted taxon — the ColDP NameUsage convention. Accepted taxa carry no parent; the classification comes entirely from the denormalised `kingdom`…`family` columns, from which CLB materialises ~113 implicit higher taxa (`usagesByOriginCount.denormed classification`).
- **`basionymID`** creates a `basionym` name relation.
- **`nameReferenceID`** must be an existing key in `reference.bib` (ColDP nomenclatural reference).
- **`status`** is `accepted` or `synonym`; `rank` is `genus`/`species`/`subspecies`.
- **`etymology`** — the person the organism is named after. This is the point of the dataset and is populated on every row. Style: bare name, with an affiliation in parentheses where it disambiguates (`Lemmy Kilmister (Motörhead)`).
- **`link`** — the ColDP taxon URL; must be a resolvable `http(s)` URI. Recent rows use the `https://doi.org/…` form.

Only terms defined in the backend's `ColdpTerm` enum are read — an unrecognised column is silently ignored on import, so verify against `~/code/col/backend/coldp/src/main/java/life/catalogue/coldp/ColdpTerm.java` before inventing one.

## Editing Conventions

- Adding a taxon: append a row to `name_usage.tsv` with the next ID, plus rows in `vernacular_name.tsv` / `media.tsv` only if that data exists. Do not invent licences or creators for images that the source does not state.
- Adding a reference: get the DOI, then
  ```sh
  echo 10.11646/zootaxa.5866.2.6 | ./lookup.sh   # appends to newrefs.bib
  ```
  reformat to the file's aligned-`=` style (CrossRef returns one long line), append to the end of `reference.bib` — the file is in append order, not sorted — and use its key as `nameReferenceID`. CrossRef's generated keys (`HUANG_2026`) are kept verbatim, which is why casing is mixed.
- Commit messages close issues on this repo's own GitHub tracker (`Add Hemiandrus jacinda, fixes #20`).

### Bulk loading with `load.py`

```sh
./load.py                 # dry run -> candidates.tsv + a summary
./load.py --limit 50      # only the first 50 new names (testing)
./load.py --apply         # append the candidates to the ColDP files
./load.py --no-refs       # skip CrossRef, leave nameReferenceID empty
```

It is **re-runnable**: every name already in `name_usage.tsv` is skipped, so a later
run only picks up names Wikipedia has gained since. All HTTP responses are cached
under `.cache/` (gitignored), so repeat runs are cheap.

What it does per row:

- **Parses the wikitables** with proper `rowspan` resolution — several rows share a
  Type/Ref cell with the row above, which shifts every later cell left. A positional
  parse without this reads the namesake out of the Type column.
- **Names**: takes the *displayed* side of `[[target|display]]` links (the target is
  often a different accepted name), strips `{{nowrap}}`/`{{small}}` wrappers, drops
  subgenera (`Agra (Agra) x` → `Agra x`), and keeps hybrids (`Kalanchoe × poincarei`).
- **`extinct`** comes from the `†` marker in the taxon cell.
- **`etymology`** is `Person: quote`, where the quote is the publication text
  Wikipedia gives in the Notes column, with its quote marks stripped. Bare `Person`
  when there is no quote. Only these facts are taken — never Wikipedia's own prose —
  which keeps the CC0 licence of this dataset clean.
- **Classification** is resolved against the current COL release via
  `api.checklistbank.org/dataset/3LR/match/nameusage`. The dry-run file records the
  match type per row: `variant` is a real hit; `higherrank` means only the genus
  matched, so `kingdom`…`family` are still right but the name itself is not in COL;
  `none` means no match and the classification columns are left empty.
- **References**: the `doi=` in the row's `{{cite}}` template is resolved to BibTeX
  via CrossRef and reformatted into this file's aligned style. DOIs already present
  in `reference.bib` reuse the existing key rather than adding a duplicate.

Every candidate is emitted as `status=accepted`. Some are synonyms in COL, but this
dataset has no accepted taxon to point a `parentID` at, and 119 of the original 122
rows follow the same "name as published" convention.

### The pre-commit hook

`.git/hooks/pre-commit` stamps `issued:`/`version:` in `metadata.yaml` with today's date. It is not versioned by git, so after a fresh clone install it:

```sh
cp pre-commit.hook .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
```

## Validation Before Committing

```sh
# every row must have 17 tab-separated fields
awk -F'\t' 'NF!=17{print NR": "NF" fields"}' name_usage.tsv

# no duplicate usage IDs
awk -F'\t' 'NR>1{print $1}' name_usage.tsv | sort | uniq -d

# no duplicate BibTeX keys
grep -o '^@[a-zA-Z]*{[^,]*' reference.bib | sed 's/.*{//' | sort | uniq -d

# nameReferenceID <-> reference.bib key cross-check (both sides should be empty)
grep -o '^@[a-zA-Z]*{[^,]*' reference.bib | sed 's/.*{//' | sort -u > /tmp/keys.txt
awk -F'\t' 'NR>1 && $10!=""{print $10}' name_usage.tsv | sort -u > /tmp/refs.txt
comm -23 /tmp/refs.txt /tmp/keys.txt   # referenced but missing
comm -13 /tmp/refs.txt /tmp/keys.txt   # defined but unused

# extension taxonIDs must resolve to an accepted usage
for f in vernacular_name.tsv media.tsv; do
  awk -F'\t' 'NR==FNR{if(FNR>1) st[$1]=$4; next}
       FNR>1 && st[$1]!="accepted"{print FILENAME" row "FNR": taxonID "$1" -> "(($1 in st)?st[$1]:"MISSING")}' \
    name_usage.tsv "$f"
done

# links must be URIs
awk -F'\t' 'NR>1 && $17!="" && $17 !~ /^https?:\/\//{print $1": "$17}' name_usage.tsv

# no CRLF crept back in
grep -lc $'\r' *.tsv *.bib *.yaml
```

## Related Context

The wider Catalogue of Life stack (backend, ChecklistBank UI, portal, coldp spec) is described in `~/.claude/CLAUDE.md`. The ColDP field definitions this dataset must conform to live in `~/code/col/coldp/README.md` (`## NameUsage`, `## VernacularName`, `## Media`, `## Reference BIBTEX`); the authoritative list of terms the importer accepts is `ColdpTerm.java` in the backend repo.
