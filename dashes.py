"""Logique pure de emdash-guard : detection et reecriture des tirets cadratins.

Stdlib uniquement, aucun import Hermes : testable seul et reappelable depuis n'importe
quel script (voir ``main`` pour l'usage en ligne de commande).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

# U+2014 em dash, U+2013 en dash, U+2015 horizontal bar : les trois tirets de la
# famille "cadratin/demi-cadratin" qu'un modele produit et qu'aucune redaction ne veut.
DASH_CHARS = "\u2014\u2013\u2015"

# Un tiret entre deux chiffres est un separateur de plage : tiret simple colle.
_RANGE_RE = __import__("re").compile(r"(?<=\d)[" + DASH_CHARS + r"](?=\d)")
# Partout ailleurs c'est une ponctuation : un seul tiret simple entoure d'espaces.
_BREAK_RE = __import__("re").compile(r"[ \t]*[" + DASH_CHARS + r"][ \t]*")

TEXT_SUFFIXES = {
    ".md", ".markdown", ".mdx", ".txt", ".rst", ".tex", ".csv", ".tsv",
    ".py", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".html", ".htm", ".css", ".scss", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".sh", ".bash", ".zsh", ".sql", ".xml", ".svg", ".env", ".properties",
}


def has_dash(text: str) -> bool:
    return any(char in text for char in DASH_CHARS)


def count_dashes(text: str) -> int:
    return sum(text.count(char) for char in DASH_CHARS)


def fix_line(line: str) -> str:
    """Reecrit une ligne : plages numeriques collees, ponctuation en ' - ', ligne detachee."""
    fixed = _RANGE_RE.sub("-", line)
    fixed = _BREAK_RE.sub(" - ", fixed)
    return fixed.rstrip()


def fix_dashes(text: str) -> tuple:
    """Retourne ``(texte_corrige, nombre_de_tirets_cadratins)``."""
    if not isinstance(text, str) or not text or not has_dash(text):
        return text, 0
    count = count_dashes(text)
    lines: List[str] = []
    for line in text.split("\n"):
        lines.append(fix_line(line) if has_dash(line) else line)
    return "\n".join(lines), count


def is_text_file(path: Path) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    try:
        with path.open("rb") as handle:
            head = handle.read(8192)
    except OSError:
        return False
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return path.suffix == ""


def _is_own_file(path: Path) -> bool:
    return path.name in {"emdash-guard.log", "emdash-guard.json"}


def iter_files(targets: Iterable[Path]) -> List[Path]:
    """Etend les chemins : dossiers parcourus recursivement, fichiers texte uniquement.

    Nos propres fichiers d'etat/journal sont exclus : ils contiennent volontairement
    les cadratins interceptes (la preuve de ce qui a ete corrige), les nettoyer ferait
    disparaitre la trace au lieu de corriger une redaction.
    """
    found: List[Path] = []
    for target in targets:
        path = Path(target).expanduser()
        if path.is_dir():
            found.extend(sorted(p for p in path.rglob("*")
                                if p.is_file() and is_text_file(p) and not _is_own_file(p)))
        elif path.is_file() and is_text_file(path) and not _is_own_file(path):
            found.append(path)
        elif not path.exists():
            print(f"emdash-guard : chemin ignore (introuvable) {path}", file=sys.stderr)
    return found


def scan_targets(targets: Iterable[Path], *, fix: bool = False) -> Dict[str, Any]:
    """Analyse les cibles ; ``fix=True`` reecrit les fichiers touches, sinon lecture seule."""
    files = iter_files(targets)
    hits: List[Dict[str, Any]] = []
    dashes = 0
    touched = 0
    for path in files:
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not has_dash(original):
            continue
        fixed, count = fix_dashes(original)
        dashes += count
        touched += 1
        for number, line in enumerate(original.split("\n"), start=1):
            if has_dash(line):
                hits.append({"path": str(path), "line": number, "text": line})
        if fix and fixed != original:
            path.write_text(fixed, encoding="utf-8")
    return {"files_scanned": len(files), "files_hit": touched, "dashes": dashes, "hits": hits}


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dashes.py",
        description="Detecte et supprime les tirets cadratins (U+2014/U+2013/U+2015).",
    )
    parser.add_argument("paths", nargs="*", help="fichiers ou dossiers (recursif)")
    parser.add_argument("--fix", action="store_true", help="reecrit sur place")
    parser.add_argument("--check", action="store_true", help="lecture seule, sortie 1 si trouve")
    parser.add_argument("--quiet", action="store_true", help="resume uniquement")
    parser.add_argument("--stdin", action="store_true", help="lit stdin et ecrit le texte corrige")
    args = parser.parse_args(argv)

    if args.stdin:
        text = sys.stdin.read()
        fixed, count = fix_dashes(text)
        sys.stdout.write(fixed)
        print(f"emdash-guard : {count} tiret(s) cadratin(s) corrige(s).", file=sys.stderr)
        return 0

    if not args.paths:
        parser.error("aucun chemin fourni (ou utiliser --stdin)")

    report = scan_targets([Path(p) for p in args.paths], fix=args.fix)
    if not args.quiet:
        for hit in report["hits"]:
            print(f"{hit['path']}:{hit['line']}: {hit['text']}")
    action = "reecrit(s)" if args.fix else "touche(s)"
    print(f"emdash-guard : {report['dashes']} tiret(s) cadratin(s) dans {report['files_hit']} "
          f"fichier(s) {action} sur {report['files_scanned']} analyse(s).")
    if args.check and report["dashes"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
