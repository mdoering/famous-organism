#!/usr/bin/env python3
"""Load organisms named after famous people from external sources into ColDP.

Currently implements the six English Wikipedia "List of organisms named after
famous people" pages. Run repeatedly: everything already present in
name_usage.tsv is skipped, so only genuinely new names are emitted.

    ./load.py                 # dry run -> candidates.tsv + a summary
    ./load.py --apply         # append the candidates to the ColDP files
    ./load.py --limit 50      # parse/resolve only the first 50 new names

Remote lookups (Wikipedia, ChecklistBank, CrossRef) are cached under .cache/
so re-runs are cheap and resumable.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(REPO, ".cache")
UA = "famous-organism-loader/1.0 (+https://github.com/mdoering/famous-organism)"

WIKI_PAGES = [
    "List of organisms named after famous people (born before 1800)",
    "List of organisms named after famous people (born 1800–1899)",
    "List of organisms named after famous people (born 1900–1924)",
    "List of organisms named after famous people (born 1925–1949)",
    "List of organisms named after famous people (born 1950–1974)",
    "List of organisms named after famous people (born 1975–present)",
]

# name_usage.tsv column order
COLUMNS = ["ID", "parentID", "basionymID", "status", "rank", "etymology",
           "scientificName", "authorship", "namePublishedInYear",
           "nameReferenceID", "extinct", "kingdom", "phylum", "class",
           "order", "family", "link"]
RANKS = ("kingdom", "phylum", "class", "order", "family")

# Wikipedia table columns
COL_TAXON, COL_TYPE, COL_NAMESAKE, COL_NOTES, COL_REF = 0, 1, 2, 3, 6
NCOLS = 7


# --------------------------------------------------------------- http cache
def cached(name, fetch):
    """Return cached bytes for `name`, calling fetch() on a miss."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, re.sub(r"[^A-Za-z0-9._-]", "_", name)[:180])
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    data = fetch()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data)
    return data


def get(url, accept=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if accept:
        req.add_header("Accept", accept)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


# ---------------------------------------------------------- wikitext parsing
def fetch_wikitext(page):
    def go():
        url = ("https://en.wikipedia.org/w/api.php?action=parse&page="
               + urllib.parse.quote(page) + "&prop=wikitext&format=json&formatversion=2")
        return json.loads(get(url))["parse"]["wikitext"]
    return cached("wiki_" + page, go)


def split_templates(text):
    """Yield (before, name, args) for each top-level {{...}}, brace-balanced."""
    out, i = [], 0
    while True:
        start = text.find("{{", i)
        if start < 0:
            out.append(("text", text[i:]))
            return out
        out.append(("text", text[i:start]))
        depth, j = 0, start
        while j < len(text):
            if text.startswith("{{", j):
                depth += 1
                j += 2
            elif text.startswith("}}", j):
                depth -= 1
                j += 2
                if depth == 0:
                    break
            else:
                j += 1
        inner = text[start + 2:j - 2]
        parts = split_top(inner, "|")
        out.append(("tmpl", parts[0].strip().lower(), parts[1:]))
        i = j


def split_top(text, sep):
    """Split on `sep` ignoring anything nested in {{ }} or [[ ]]."""
    parts, depth, cur = [], 0, []
    i = 0
    while i < len(text):
        if text.startswith("{{", i) or text.startswith("[[", i):
            depth += 1
            cur.append(text[i:i + 2])
            i += 2
        elif text.startswith("}}", i) or text.startswith("]]", i):
            depth -= 1
            cur.append(text[i:i + 2])
            i += 2
        elif depth == 0 and text.startswith(sep, i):
            parts.append("".join(cur))
            cur = []
            i += len(sep)
        else:
            cur.append(text[i])
            i += 1
    parts.append("".join(cur))
    return parts


def clean(text):
    """Reduce wikitext to plain text."""
    if not text:
        return ""
    text = re.sub(r"<ref[^>]*/>", "", text)
    text = re.sub(r"<ref.*?</ref>", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<br\s*/?>", " ", text)
    text = re.sub(r"<[^>]+>", "", text)

    out = []
    for item in split_templates(text):
        if item[0] == "text":
            out.append(item[1])
            continue
        name, args = item[1], item[2]
        pos = [a for a in args if "=" not in a.split("|")[0][:20]]
        if name == "sortname":
            out.append(" ".join(a.strip() for a in pos[:2]))
        elif name in ("small", "nowrap", "nobr", "no wrap", "lang", "'"):
            out.append(clean(pos[-1]) if pos else "")
        # every other template (cite, flagicon, efn, ...) is dropped
    text = "".join(out)

    # links: [[target|display]] -> display, [[target]] -> target
    text = re.sub(r"\[\[([^\]\|]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[https?://\S+\s+([^\]]+)\]", r"\1", text)
    text = text.replace("'''", "").replace("''", "")
    text = text.replace("&nbsp;", " ").replace("\u00a0", " ")
    text = text.replace("\ufeff", "").replace("\u200b", "").replace("\u00ad", "")
    text = text.replace("&amp;", "&").replace("&quot;", '"')
    return re.sub(r"\s+", " ", text).strip()


def parse_rows(wikitext):
    """Yield 7-column rows from every wikitable, resolving rowspan."""
    for table in re.findall(r"\n\{\|.*?\n\|\}", wikitext, flags=re.S):
        pending = {}          # col index -> [value, rows_remaining]
        chunks = re.split(r"\n\|-[^\n]*", table)[1:]
        for chunk in chunks:
            chunk = chunk.split("\n|}")[0]
            body = chunk.lstrip("\n")
            if body.startswith("!"):     # header row
                continue
            body = body.lstrip("|")
            cells = split_top(body, "||")
            row, ci = [], 0
            for col in range(NCOLS):
                if col in pending:
                    row.append(pending[col][0])
                    pending[col][1] -= 1
                    if pending[col][1] <= 0:
                        del pending[col]
                    continue
                if ci >= len(cells):
                    row.append("")
                    continue
                cell = cells[ci]
                ci += 1
                m = re.match(r'\s*rowspan\s*=\s*"?(\d+)"?\s*\|(.*)', cell, flags=re.S)
                if m:
                    span, cell = int(m.group(1)), m.group(2)
                    if span > 1:
                        pending[col] = [cell, span - 1]
                else:
                    cell = re.sub(r'^\s*(?:align|style|class|id)\s*=\s*"[^"]*"\s*\|',
                                  "", cell, flags=re.S)
                row.append(cell)
            if any(c.strip() for c in row):
                yield row


# ------------------------------------------------------------ field pulling
NAME_RE = re.compile(
    r"^[A-Z][a-z\u00e0-\u00ff]+"                                  # genus
    r"(?:\s+(?:\u00d7\s+)?[a-z\u00e0-\u00ff][a-z\u00e0-\u00ff'\u2019.-]*){0,2}"  # epithets
    r"(?:\s+[IVX]{1,4})?$"                                     # historic "gulielmi III"
)
SUBGENUS_RE = re.compile(r"\s*\(\s*[A-Z][a-z\u00e0-\u00ff]+\s*\)")


def parse_taxon(cell):
    """-> (scientificName, authorship, extinct)"""
    extinct = "†" in cell
    authorship = ""
    for item in split_templates(cell):
        if item[0] == "tmpl" and item[1] == "small" and item[2]:
            authorship = clean(item[2][-1])
    # the name is the italicised part, minus the {{small|...}} authorship
    stripped = re.sub(r"\{\{\s*small\s*\|.*?\}\}", "", cell, flags=re.S)
    m = re.search(r"''\s*(?:\[\[([^\]\|]+)\|([^\]]+)\]\]|\[\[([^\]]+)\]\]|([^']+?))\s*''",
                  stripped)
    if m:
        name = m.group(2) or m.group(3) or m.group(4) or ""
    else:
        name = clean(stripped)
    name = clean(name).replace("†", "").strip()
    name = SUBGENUS_RE.sub("", name).strip()      # "Agra (Agra) x" -> "Agra x"
    return name, authorship, extinct


def parse_quote(notes):
    """The publication quote inside the Notes cell, without its quote marks."""
    txt = clean(notes).replace("“", '"').replace("”", '"')
    first, last = txt.find('"'), txt.rfind('"')
    if first < 0 or last <= first:
        return ""
    quote = txt[first + 1:last].strip()
    return re.sub(r'\s+', " ", quote).strip(' .,;')


def parse_doi(refcell):
    m = re.search(r"\|\s*doi\s*=\s*([^\|\}\s]+)", refcell, flags=re.I)
    if m:
        return m.group(1).strip().rstrip(".,")
    m = re.search(r"doi\.org/([^\s\|\}\]]+)", refcell, flags=re.I)
    return m.group(1).strip().rstrip(".,") if m else ""


def parse_year(authorship):
    m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", authorship)
    return m.group(1) if m else ""


# ----------------------------------------------------------- remote services
def col_match(name):
    def go():
        url = ("https://api.checklistbank.org/dataset/3LR/match/nameusage?q="
               + urllib.parse.quote(name))
        return get(url)
    try:
        return json.loads(cached("col_" + name, go))
    except Exception:
        return {}


def classification(name):
    """-> (dict of kingdom..family, matchType, matchedRank)"""
    d = col_match(name)
    usage = d.get("usage")
    if not usage:
        return {}, d.get("type", "none"), ""
    cl = {c.get("rank"): c.get("name") for c in usage.get("classification", [])}
    if usage.get("rank") in RANKS:
        cl[usage["rank"]] = usage.get("name")
    return ({r: cl.get(r, "") for r in RANKS}, d.get("type", ""), usage.get("rank", ""))


def crossref_bibtex(doi):
    def go():
        return get("https://doi.org/" + urllib.parse.quote(doi),
                   accept="application/x-bibtex")
    try:
        txt = cached("doi_" + doi, go)
        return txt if txt.lstrip().startswith("@") else ""
    except Exception:
        return ""


def _bib_fields(body):
    """Split a BibTeX entry body on commas that sit at brace depth 0."""
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth <= 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


BARE = ("year", "volume", "number", "month")
FIELD_ORDER = ["title", "author", "editor", "year", "month", "journal", "booktitle",
               "publisher", "volume", "number", "pages", "doi", "issn", "isbn", "url"]


KEY_OK = re.compile(r"^[A-Za-z0-9_.:+/-]+$")


def safe_key(key, fields):
    """A BibTeX key may not contain whitespace; some publishers emit author
    names as keys, which aborts the parse of the whole file in ChecklistBank.
    Rebuild those as Surname_Year."""
    if KEY_OK.match(key):
        return key
    author = fields.get("author", "").strip("{}")
    surname = re.split(r"\s*(?:,| and )", author)[0].strip()
    surname = re.sub(r"[^A-Za-z0-9]", "", surname) or "ref"
    year = re.sub(r"[^0-9]", "", fields.get("year", "")) or "0000"
    return f"{surname}_{year}"


def tidy_bibtex(raw):
    """Reformat CrossRef's one-liner into the aligned style used in reference.bib."""
    raw = raw.strip()
    m = re.match(r"@(\w+)\s*\{\s*([^,]+),(.*)\}\s*$", raw, flags=re.S)
    if not m:
        return "", ""
    kind, key, body = m.group(1).lower(), m.group(2).strip(), m.group(3)

    fields = []
    for part in _bib_fields(body):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip().lower(), v.strip()
        v = re.sub(r"<[^>]+>", "", v)              # CrossRef leaks <i> into titles
        v = re.sub(r"\s+", " ", v).strip()
        inner = v[1:-1].strip() if v.startswith("{") and v.endswith("}") else v
        if k in BARE and re.fullmatch(r"[A-Za-z0-9]+", inner):
            v = inner.lower()[:3] if k == "month" else inner
        else:
            v = "{" + inner + "}"
        fields.append((k, v))

    fields.sort(key=lambda kv: (FIELD_ORDER.index(kv[0])
                                if kv[0] in FIELD_ORDER else 99, kv[0]))

    key = safe_key(key, dict(fields))

    lines = [f"@{kind}{{{key},"]
    for i, (k, v) in enumerate(fields):
        comma = "," if i < len(fields) - 1 else ""
        lines.append(f"\t{k:<12} = {v}{comma}")
    lines.append("}")
    return key, "\n".join(lines)


# ------------------------------------------------------------------- ColDP io
def read_tsv(path):
    with open(path, encoding="utf-8") as fh:
        rows = [l.rstrip("\n").split("\t") for l in fh if l.strip()]
    return rows[0], rows[1:]


def existing_bib(path):
    """-> (set of keys, dict doi -> key)"""
    text = open(path, encoding="utf-8").read()
    keys, by_doi = set(), {}
    for entry in re.split(r"(?m)^(?=@)", text):
        m = re.match(r"@\w+\{([^,]+),", entry)
        if not m:
            continue
        key = m.group(1).strip()
        keys.add(key)
        d = re.search(r"(?m)^\s*doi\s*=\s*\{?([^\},\n]+)", entry, flags=re.I)
        if d:
            by_doi[d.group(1).strip().lower()] = key
    return keys, by_doi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="wikipedia", choices=["wikipedia"])
    ap.add_argument("--limit", type=int, default=0, help="cap new names (testing)")
    ap.add_argument("--apply", action="store_true",
                    help="append to name_usage.tsv and reference.bib")
    ap.add_argument("--no-refs", action="store_true",
                    help="skip CrossRef lookups (leave nameReferenceID empty)")
    args = ap.parse_args()

    nu_path = os.path.join(REPO, "name_usage.tsv")
    bib_path = os.path.join(REPO, "reference.bib")
    header, rows = read_tsv(nu_path)
    if header != COLUMNS:
        sys.exit("name_usage.tsv header changed - update COLUMNS in load.py")

    have = {r[COLUMNS.index("scientificName")].strip().lower() for r in rows}
    next_id = max(int(r[0]) for r in rows if r[0].isdigit()) + 1
    bib_keys, bib_by_doi = existing_bib(bib_path)

    # ---- parse every source page
    seen, candidates, stats = set(), [], {"rows": 0, "dupe": 0, "nolink": 0}
    for page in WIKI_PAGES:
        for row in parse_rows(fetch_wikitext(page)):
            stats["rows"] += 1
            name, authorship, extinct = parse_taxon(row[COL_TAXON])
            if not name or not NAME_RE.match(name):
                stats["nolink"] += 1
                continue
            key = name.lower()
            if key in have or key in seen:
                stats["dupe"] += 1
                continue
            seen.add(key)
            person = clean(row[COL_NAMESAKE])
            quote = parse_quote(row[COL_NOTES])
            candidates.append({
                "name": name, "authorship": authorship, "extinct": extinct,
                "person": person, "quote": quote, "doi": parse_doi(row[COL_REF]),
                "page": page,
            })

    if args.limit:
        candidates = candidates[:args.limit]

    # ---- resolve classification and references
    new_bib, unmatched = [], 0
    for i, c in enumerate(candidates, 1):
        cl, mtype, mrank = classification(c["name"])
        c["cl"], c["matchType"], c["matchRank"] = cl, mtype, mrank
        if not cl:
            unmatched += 1
        words = len([w for w in c["name"].split() if w != "\u00d7"])
        c["rank"] = {1: "genus", 2: "species", 3: "subspecies"}[min(words, 3)]
        if mtype not in ("none", "higherrank") and mrank in (
                "genus", "species", "subspecies"):
            c["rank"] = mrank

        c["refkey"] = ""
        if c["doi"] and not args.no_refs:
            doi = c["doi"].lower()
            if doi in bib_by_doi:
                c["refkey"] = bib_by_doi[doi]
            else:
                key, entry = tidy_bibtex(crossref_bibtex(c["doi"]))
                if key:
                    while key in bib_keys:          # CrossRef keys can collide
                        key += "a"
                        entry = re.sub(r"^@(\w+)\{[^,]+,", rf"@\1{{{key},", entry)
                    bib_keys.add(key)
                    bib_by_doi[doi] = key
                    new_bib.append(entry)
                    c["refkey"] = key
        if i % 100 == 0:
            print(f"  resolved {i}/{len(candidates)}", file=sys.stderr)

    # ---- build ColDP rows
    out = []
    for c in candidates:
        etym = c["person"] + (": " + c["quote"] if c["quote"] else "")
        rec = dict.fromkeys(COLUMNS, "")
        rec.update({
            "ID": str(next_id), "status": "accepted", "rank": c["rank"],
            "etymology": etym, "scientificName": c["name"],
            "authorship": c["authorship"],
            "namePublishedInYear": parse_year(c["authorship"]),
            "nameReferenceID": c["refkey"],
            "extinct": "TRUE" if c["extinct"] else "FALSE",
            "link": "https://doi.org/" + c["doi"] if c["doi"] else "",
        })
        rec.update({r: c["cl"].get(r, "") for r in RANKS})
        out.append([rec[col] for col in COLUMNS])
        next_id += 1

    # ---- report
    print(f"source rows parsed : {stats['rows']}")
    print(f"  unusable taxon   : {stats['nolink']}")
    print(f"  already present  : {stats['dupe']}")
    print(f"new candidates     : {len(out)}")
    print(f"  no COL match     : {unmatched}")
    print(f"  with a reference : {sum(1 for c in candidates if c['refkey'])}")
    print(f"new bibtex entries : {len(new_bib)}")

    if args.apply:
        with open(nu_path, "a", encoding="utf-8", newline="\n") as fh:
            for r in out:
                fh.write("\t".join(r) + "\n")
        if new_bib:
            with open(bib_path, "a", encoding="utf-8", newline="\n") as fh:
                for e in new_bib:
                    fh.write(e + "\n")
        print(f"\nappended {len(out)} rows to name_usage.tsv "
              f"and {len(new_bib)} entries to reference.bib")
    else:
        prev = os.path.join(REPO, "candidates.tsv")
        with open(prev, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\t".join(COLUMNS) + "\tmatchType\tmatchRank\n")
            for r, c in zip(out, candidates):
                fh.write("\t".join(r) + f"\t{c['matchType']}\t{c['matchRank']}\n")
        print(f"\ndry run - wrote {prev}\nre-run with --apply to append them")


if __name__ == "__main__":
    main()
