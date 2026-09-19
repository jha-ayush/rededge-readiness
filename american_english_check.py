#!/usr/bin/env python3
"""
american_english_check.py

Guards the spelling of the tree: RedEdge Readiness is written in American
English, and this fails the build on a British spelling anywhere in it.

The product is for pilots in the United States, and its three clients and
their documents read as one voice or they do not. A first pass on
September 19, 2026 found and corrected the drift by hand ("enquiries", "belt
and braces"); the sibling project found 1,612 such words across 240 files
that had accumulated one commit at a time with nothing looking. This is the
thing that looks.

Every text file (Markdown, Python, JavaScript, HTML, YAML, the Cloudflare
config files) is read word by word against an explicit list of British
spellings and their American forms. The list is explicit, not stem-guessed:
"optimistic", "emphasis", "analysis" and "practice" are not matches. Tokens
whose spelling is part of someone else's contract (`aria-labelledby`) are
protected in full. A floor on the number of files scanned turns a broken
walk into a red run rather than a clean one.

Zero dependencies: Python stdlib only, a development and CI tool, never a
runtime requirement for the field tools.

Run:
  python3 american_english_check.py
Exit code: 0 clean, 1 a British spelling was found (listed with its
replacement), 2 the guard could not run.
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".wrangler", "_mock_pull"}
SKIP_FILES = {"american_english_check.py"}  # names every British form on purpose
EXT = {".md", ".py", ".js", ".mjs", ".html", ".yml", ".yaml", ".jsonc", ".json", ".css", ".txt"}
NAMES = {"_headers", "_redirects", ".gitignore"}
MIN_FILES = 15  # the tree held 19 such files on September 19, 2026
PROTECT = re.compile(r"(aria-labelledby|labelledby|labelledBy|arialabelledby)")


def _forms(uk: str, us: str, suffixes) -> dict:
    return {uk + s: us + s for s in suffixes}


def british_to_american() -> dict:
    m: dict = {}
    ise = ["e", "es", "ed", "ing", "er", "ers", "ation", "ations", "able"]
    for uk, us in [
        ("authoris", "authoriz"),
        ("organis", "organiz"),
        ("recognis", "recogniz"),
        ("realis", "realiz"),
        ("optimis", "optimiz"),
        ("minimis", "minimiz"),
        ("maximis", "maximiz"),
        ("normalis", "normaliz"),
        ("serialis", "serializ"),
        ("initialis", "initializ"),
        ("sanitis", "sanitiz"),
        ("summaris", "summariz"),
        ("utilis", "utiliz"),
        ("categoris", "categoriz"),
        ("prioritis", "prioritiz"),
        ("customis", "customiz"),
        ("standardis", "standardiz"),
        ("visualis", "visualiz"),
        ("finalis", "finaliz"),
        ("emphasis", "emphasiz"),
        ("specialis", "specializ"),
        ("capitalis", "capitaliz"),
        ("generalis", "generaliz"),
        ("itemis", "itemiz"),
        ("stabilis", "stabiliz"),
        ("localis", "localiz"),
        ("synchronis", "synchroniz"),
        ("memoris", "memoriz"),
        ("apologis", "apologiz"),
        ("neutralis", "neutraliz"),
        ("centralis", "centraliz"),
        ("formalis", "formaliz"),
        ("penalis", "penaliz"),
        ("tokenis", "tokeniz"),
        ("randomis", "randomiz"),
        ("harmonis", "harmoniz"),
        ("modernis", "moderniz"),
        ("characteris", "characteriz"),
        ("rationalis", "rationaliz"),
        ("familiaris", "familiariz"),
        ("crystallis", "crystalliz"),
        ("mobilis", "mobiliz"),
    ]:
        m.update(_forms(uk, us, ise))
    m.update(
        {
            "analyse": "analyze",
            "analyses": "analyzes",
            "analysed": "analyzed",
            "analysing": "analyzing",
            "analyser": "analyzer",
            "paralyse": "paralyze",
            "paralysed": "paralyzed",
            "catalyse": "catalyze",
        }
    )
    our = ["", "s", "ed", "ing", "ful", "less", "able", "ite", "ites", "al", "ally", "hood", "hoods"]
    for uk, us in [
        ("colour", "color"),
        ("behaviour", "behavior"),
        ("favour", "favor"),
        ("honour", "honor"),
        ("labour", "labor"),
        ("neighbour", "neighbor"),
        ("harbour", "harbor"),
        ("humour", "humor"),
        ("flavour", "flavor"),
        ("armour", "armor"),
        ("endeavour", "endeavor"),
        ("vapour", "vapor"),
        ("savour", "savor"),
        ("rumour", "rumor"),
        ("odour", "odor"),
        ("vigour", "vigor"),
        ("rigour", "rigor"),
        ("candour", "candor"),
        ("valour", "valor"),
    ]:
        m.update(_forms(uk, us, our))
    m.update(
        {
            "discolour": "discolor",
            "discoloured": "discolored",
            "discolouration": "discoloration",
            "colourless": "colorless",
            "favourable": "favorable",
            "favourably": "favorably",
            "honourable": "honorable",
            "behavioural": "behavioral",
            "behaviourally": "behaviorally",
            "labourer": "laborer",
            "neighbouring": "neighboring",
            "colouration": "coloration",
        }
    )
    for uk, us in [
        ("centre", "center"),
        ("kilometre", "kilometer"),
        ("litre", "liter"),
        ("theatre", "theater"),
        ("fibre", "fiber"),
        ("metre", "meter"),
        ("millimetre", "millimeter"),
        ("centimetre", "centimeter"),
        ("calibre", "caliber"),
        ("sombre", "somber"),
        ("lustre", "luster"),
        ("spectre", "specter"),
        ("manoeuvre", "maneuver"),
    ]:
        m.update(_forms(uk, us, ["", "s"]))
    m.update(
        {
            "centred": "centered",
            "centring": "centering",
            "centrepiece": "centerpiece",
            "manoeuvred": "maneuvered",
            "manoeuvring": "maneuvering",
            "manoeuvrable": "maneuverable",
        }
    )
    m.update(
        {
            "travelled": "traveled",
            "travelling": "traveling",
            "traveller": "traveler",
            "travellers": "travelers",
            "cancelled": "canceled",
            "cancelling": "canceling",
            "modelled": "modeled",
            "modelling": "modeling",
            "labelled": "labeled",
            "labelling": "labeling",
            "labellings": "labelings",
            "signalled": "signaled",
            "signalling": "signaling",
            "totalled": "totaled",
            "totalling": "totaling",
            "fuelled": "fueled",
            "fuelling": "fueling",
            "levelled": "leveled",
            "levelling": "leveling",
            "marvellous": "marvelous",
            "jewellery": "jewelry",
            "counsellor": "counselor",
            "counsellors": "counselors",
            "channelled": "channeled",
            "channelling": "channeling",
            "dialled": "dialed",
            "dialling": "dialing",
            "tunnelled": "tunneled",
            "tunnelling": "tunneling",
            "pencilled": "penciled",
            "unlabelled": "unlabeled",
            "relabelled": "relabeled",
            "relabelling": "relabeling",
            "mislabelled": "mislabeled",
            "mislabelling": "mislabeling",
            "remodelled": "remodeled",
            "remodelling": "remodeling",
            "equalled": "equaled",
            "equalling": "equaling",
            "rivalled": "rivaled",
            "spiralled": "spiraled",
            "spiralling": "spiraling",
            "trialled": "trialed",
            "trialling": "trialing",
            "fulfil": "fulfill",
            "fulfils": "fulfills",
            "enrol": "enroll",
            "enrols": "enrolls",
            "instalment": "installment",
            "instalments": "installments",
            "skilful": "skillful",
            "wilful": "willful",
            "distil": "distill",
            "instil": "instill",
        }
    )
    m.update(
        {
            "licence": "license",
            "licences": "licenses",
            "defence": "defense",
            "defences": "defenses",
            "offence": "offense",
            "offences": "offenses",
            "pretence": "pretense",
            "practise": "practice",
            "practised": "practiced",
            "practising": "practicing",
            "practises": "practices",
            "grey": "gray",
            "greys": "grays",
            "greyed": "grayed",
            "greying": "graying",
            "greyish": "grayish",
            "greyscale": "grayscale",
            "catalogue": "catalog",
            "catalogues": "catalogs",
            "catalogued": "cataloged",
            "cataloguing": "cataloging",
            "programme": "program",
            "programmes": "programs",
            "tyre": "tire",
            "tyres": "tires",
            "kerb": "curb",
            "kerbs": "curbs",
            "aluminium": "aluminum",
            "cheque": "check",
            "cheques": "checks",
            "mould": "mold",
            "moulds": "molds",
            "moulded": "molded",
            "plough": "plow",
            "storey": "story",
            "storeys": "stories",
            "sceptic": "skeptic",
            "sceptical": "skeptical",
            "scepticism": "skepticism",
            "enquire": "inquire",
            "enquired": "inquired",
            "enquiring": "inquiring",
            "enquiry": "inquiry",
            "enquiries": "inquiries",
            "artefact": "artifact",
            "artefacts": "artifacts",
            "aeroplane": "airplane",
            "aeroplanes": "airplanes",
            "whilst": "while",
            "amongst": "among",
            "learnt": "learned",
            "spelt": "spelled",
            "dreamt": "dreamed",
            "focussed": "focused",
            "focussing": "focusing",
            "cosy": "cozy",
            "ageing": "aging",
            "pyjamas": "pajamas",
            "draught": "draft",
            "draughts": "drafts",
            "encyclopaedia": "encyclopedia",
            "mediaeval": "medieval",
            "paediatric": "pediatric",
            "orientated": "oriented",
            "acclimatise": "acclimatize",
            "smoulder": "smolder",
            "smouldering": "smoldering",
            "sulphur": "sulfur",
            "judgement": "judgment",
            "judgements": "judgments",
        }
    )
    return m


MAP = british_to_american()
WORD = re.compile(r"[A-Za-z]+")
# Day-first dates ("13 September 2026") are the British order; every date a
# pilot sees is month first ("September 13, 2026"), and prose follows suit.
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DAY_FIRST = re.compile(r"(?<![\d/\-])\b([1-9]|[12]\d|3[01])(?:st|nd|rd|th)? (" + MONTHS + r"),? (\d{4})\b")


def scan_line(line: str):
    for m in DAY_FIRST.finditer(line):
        yield m.group(0), f"{m.group(2)} {m.group(1)}, {m.group(3)}"
    for i, part in enumerate(PROTECT.split(line)):
        if i % 2 == 1:
            continue
        for w in WORD.findall(part):
            us = MAP.get(w.lower())
            if us is not None:
                yield w, us


def files_to_scan():
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            if f in SKIP_FILES:
                continue
            if os.path.splitext(f)[1] not in EXT and f not in NAMES:
                continue
            yield os.path.relpath(os.path.join(dp, f), ROOT)


def main() -> int:
    scanned = 0
    findings = []
    for rel in files_to_scan():
        try:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                lines = fh.read().split("\n")
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1
        for n, line in enumerate(lines, 1):
            for uk, us in scan_line(line):
                findings.append((rel, n, uk, us))
    if scanned < MIN_FILES:
        print("AMERICAN ENGLISH GUARD FAIL: scanned %d files; at least %d exist. The walk is broken." % (scanned, MIN_FILES))
        return 2
    if findings:
        print("AMERICAN ENGLISH GUARD FAIL: %d British spelling(s) in %d file(s):" % (len(findings), len({f[0] for f in findings})))
        for rel, n, uk, us in findings:
            print("  %s:%d: %s -> %s" % (rel, n, uk, us))
        return 1
    print("American English guard OK: no British spellings in %d files (%d forms checked)." % (scanned, len(MAP)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
