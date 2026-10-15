"""emdash-guard - suppression deterministe des tirets cadratins (em/en dash).

Le tiret cadratin n'est jamais un choix : c'est un accident typographique de modele
de langage. Ce plugin le retire de la source, sans inference supplementaire :

* ``transform_llm_output`` (hook Hermes) reecrit CHAQUE reponse finale de l'assistant
  avant qu'elle soit stockee et livree (CLI, gateway, desktop). U+2014 em dash,
  U+2013 en dash et U+2015 horizontal bar deviennent un tiret simple ``-``.
* l'evenement est journalise bruyamment (log JSONL + banniere stderr + log Hermes)
  et signale par une notification macOS (au plus une par minute).
* ``/dashes`` affiche le compteur et permet de nettoyer des fichiers
  (``/dashes fix <chemin>...``). Meme logique en CLI : ``dashes.py [--fix] PATH...``.

Aucune dependance : stdlib uniquement, logique pure dans ``dashes.py``.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# --- logique partagee (chargee par chemin : le plugin peut etre importe en paquet ou en module nu)

_spec = importlib.util.spec_from_file_location("emdash_guard_dashes", Path(__file__).with_name("dashes.py"))
_dashes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dashes)  # type: ignore[union-attr]

DASH_CHARS: str = _dashes.DASH_CHARS
has_dash = _dashes.has_dash
fix_dashes = _dashes.fix_dashes
scan_targets = _dashes.scan_targets

LOG_FILENAME = "emdash-guard.log"
STATE_FILENAME = "emdash-guard.json"
_NOTIFY_MIN_INTERVAL = 60.0
# Notification macOS : sur opt-in uniquement (Yoan n'en veut pas au quotidien).
# HERMES_EMDASH_NOTIFY=1 la reactive ; le log et la banniere stderr restent toujours actifs.
_ENV_NOTIFY = "HERMES_EMDASH_NOTIFY"
_ENV_ANNOUNCE = "HERMES_EMDASH_ANNOUNCE"  # "1" -> marqueur visible en fin de reponse

_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {"turns": 0, "dashes": 0, "last": None, "since": None}
# Sentinelle -inf : un monotonic() qui repart de zero (process frais) ne doit pas
# declencher le throttle sur la toute premiere interception.
_LAST_NOTIFY = float("-inf")
_ANSI = sys.stderr.isatty()


def _home() -> Path:
    """Home du profil qui HEBERGE cette copie du plugin, jamais celui du process.

    Un process multiplexe sert tous les profils avec le home de LANCEMENT dans son
    environnement : les donnees du plugin doivent donc aller a cote du plugin lui-meme
    (``<home>/plugins/emdash-guard/``), sans dependre de l'environnement ni du scope
    courant. La copie hors profil (repo, tests) retombe sur ``get_hermes_home()`` puis
    sur ``HERMES_HOME``, ce qui garde l'isolation des tests.
    """
    plugin_dir = Path(__file__).resolve().parent
    if plugin_dir.parent.name == "plugins":  # <home>/plugins/<name>/__init__.py
        return plugin_dir.parent.parent
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home()).expanduser()
    except Exception:
        pass
    env = os.environ.get("HERMES_HOME")
    return Path(env).expanduser() if env else Path.home() / ".hermes"


def _log_path() -> Path:
    return _home() / "logs" / LOG_FILENAME


def _state_path() -> Path:
    return _home() / "state" / STATE_FILENAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sample(text: str, width: int = 40) -> str:
    """Extrait le premier dashes cadratin avec son contexte, sur une seule ligne."""
    for index, char in enumerate(text):
        if char in DASH_CHARS:
            start = max(0, index - width)
            end = min(len(text), index + width + 1)
            snippet = text[start:end].replace("\n", " ")
            return ("..." if start else "") + snippet + ("..." if end < len(text) else "")
    return ""


def _load_state() -> None:
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return
    if isinstance(raw, dict):
        for key in ("turns", "dashes"):
            value = raw.get(key)
            if isinstance(value, int):
                _STATS[key] = value
        if isinstance(raw.get("last"), dict):
            _STATS["last"] = raw["last"]
        if isinstance(raw.get("since"), str):
            _STATS["since"] = raw["since"]


def _persist_state() -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(_STATS)
        payload["updated_at"] = _now_iso()
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        logger.debug("emdash-guard: state write failed", exc_info=True)


def _append_log(entry: Dict[str, Any]) -> None:
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("emdash-guard: log append failed", exc_info=True)


def _notify_enabled() -> bool:
    if sys.platform != "darwin":
        return False
    return os.environ.get(_ENV_NOTIFY, "").strip().lower() in {"1", "true", "yes", "on"}


def _shout(count: int, sample: str, platform: str, session_id: str) -> None:
    """Le 'gueule' : log Hermes, banniere stderr, notification macOS (throttlee)."""
    label = f"emdash-guard: {count} tiret(s) cadratin(s) converti(s) en tiret(s) simple(s)"
    where = f" [plateforme={platform or 'inconnue'} session={session_id or '-'}]"
    logger.warning("%s%s dans : %s", label, where, sample)
    banner = f"  !! {label}  "
    if _ANSI:
        print(f"\n\033[1;41m{banner}\033[0m\n  contexte : {sample}\n", file=sys.stderr, flush=True)
    else:
        print(f"\n!! {label}\n   contexte : {sample}{where}\n", file=sys.stderr, flush=True)

    global _LAST_NOTIFY
    if not _notify_enabled():
        return
    now = time.monotonic()
    with _LOCK:
        if now - _LAST_NOTIFY < _NOTIFY_MIN_INTERVAL:
            return
        _LAST_NOTIFY = now
    text = f"{count} tiret(s) cadratin(s) corrige(s)." + (f" {sample}" if sample else "")
    text = text.encode("ascii", "replace").decode("ascii")[:200]
    apple = text.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{apple}" with title "emdash-guard" sound name "Funk"'
    try:
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
    except Exception:
        logger.debug("emdash-guard: notification failed", exc_info=True)


def _announce_enabled() -> bool:
    return os.environ.get(_ENV_ANNOUNCE, "").strip().lower() in {"1", "true", "yes", "on"}


def transform_llm_output(response_text: str = "", session_id: str = "", model: str = "",
                         platform: str = "", **kwargs: Any) -> Optional[str]:
    """Hook Hermes : reecrit la reponse finale. ``None`` = inchangee."""
    try:
        if not isinstance(response_text, str) or not response_text:
            return None
        fixed, count = fix_dashes(response_text)
        if not count or fixed == response_text:
            return None
        sample = _sample(response_text)
        entry = {
            "ts": _now_iso(), "count": count, "platform": platform or "", "session_id": session_id or "",
            "model": model or "", "chars_before": len(response_text), "chars_after": len(fixed),
            "sample": sample,
            # Diagnostic multi-process : un process multiplexe sert plusieurs profils, donc
            # savoir QUEL process et QUEL home ont intercepte est la seule facon de deboguer
            # un journal qui semble manquer une entree.
            "pid": os.getpid(), "home": str(_home()), "turn_id": str(kwargs.get("turn_id") or ""),
        }
        with _LOCK:
            _STATS["turns"] = int(_STATS.get("turns", 0)) + 1
            _STATS["dashes"] = int(_STATS.get("dashes", 0)) + count
            _STATS["last"] = entry
            _STATS["since"] = _STATS.get("since") or entry["ts"]
            _append_log(entry)
            _persist_state()
        _shout(count, sample, platform, session_id)
        if _announce_enabled():
            return fixed + f"\n\n_[emdash-guard : {count} tiret(s) cadratin(s) converti(s) en tiret(s) simple(s)]_"
        return fixed
    except Exception:  # ne jamais casser un tour
        logger.warning("emdash-guard: transform failed", exc_info=True)
        return None


def _status() -> str:
    line = (f"emdash-guard : {_STATS.get('dashes', 0)} tiret(s) cadratin(s) intercepte(s) "
            f"sur {_STATS.get('turns', 0)} reponse(s).")
    last = _STATS.get("last")
    if isinstance(last, dict):
        line += (f"\nDernier : {last.get('ts', '?')} ({last.get('count', '?')} fois, "
                 f"plateforme {last.get('platform') or 'inconnue'})\n  {last.get('sample', '')}")
    else:
        line += "\nAucune interception depuis l'installation : la sortie est propre."
    line += f"\nJournal : {_log_path()}"
    line += ("\nNotification macOS : desactivee (HERMES_EMDASH_NOTIFY=1 pour l'activer)."
             if not _notify_enabled() else "\nNotification macOS : active (max 1/minute).")
    line += f"\nRegle : U+2014/U+2013/U+2015 -> '-' (plage numerique collee, sinon ' - ')."
    return line


def _tail_log(count: int = 10) -> str:
    try:
        lines = _log_path().read_text(encoding="utf-8").splitlines()
    except Exception:
        return "emdash-guard : journal vide."
    recent = lines[-max(1, count):]
    if not recent:
        return "emdash-guard : journal vide."
    return "Dernieres interceptions :\n" + "\n".join(
        f"  {e.get('ts', '?')} - {e.get('count', '?')} fois - {e.get('sample', '')}"
        for e in (json.loads(line) for line in recent)
    )


def _command(raw_args: str) -> str:
    """``/dashes [status|log [n]|fix <chemin>...]``"""
    text = (raw_args or "").strip()
    if not text or text.lower() == "status":
        return _status()
    verb, _, rest = text.partition(" ")
    verb = verb.lower()
    if verb == "log":
        try:
            count = int(rest.strip() or "10")
        except ValueError:
            count = 10
        return _tail_log(count)
    if verb == "help":
        return "Usage : /dashes [status] | /dashes log [n] | /dashes fix <chemin>..."
    if verb in {"fix", "scan", "check"}:
        targets = [item for item in rest.split() if item]
        if not targets:
            return "Usage : /dashes fix <fichier|dossier>..."
        report = scan_targets([Path(t).expanduser() for t in targets], fix=(verb == "fix"))
        lines = [f"{report['dashes']} tiret(s) cadratin(s) dans {report['files_hit']} fichier(s) "
                 f"(sur {report['files_scanned']} analyse(s))."]
        for hit in report["hits"][:20]:
            lines.append(f"  {hit['path']}:{hit['line']} {hit['text'][:100]}")
        if len(report["hits"]) > 20:
            lines.append(f"  ... et {len(report['hits']) - 20} autre(s).")
        if verb == "fix":
            lines.append("Fichiers reecrits. Relance /dashes fix pour verifier (doit retourner 0).")
        elif verb == "check":
            lines.append("Rien n'a ete modifie.")
        return "\n".join(lines)
    return f"Argument inconnu : {verb}. Usage : /dashes [status] | /dashes log [n] | /dashes fix <chemin>..."


def register(ctx) -> None:
    _load_state()
    ctx.register_hook("transform_llm_output", transform_llm_output)
    ctx.register_command(
        "dashes", _command,
        description="Tirets cadratins : compteur emdash-guard, journal, nettoyage de fichiers.",
        args_hint="[status|log [n]|fix <chemin>...]",
    )
    logger.info("emdash-guard active (hook transform_llm_output + commande /dashes)")
