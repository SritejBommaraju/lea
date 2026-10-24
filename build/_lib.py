"""Shared helpers for build/doctor.py, build/next.py and build/status.py. Stdlib only.

- a parser for the strict YAML subset used by build/tasks.yaml and config/providers.yaml
- loaders that apply card defaults and validate the registry
- provider profiles (config/providers.yaml `profiles:`; env REPAIROS_PROFILE picks one, REPAIROS_SLOT_* overrides)
- which cards are in play (selected implementation, track) and the critical path in hours or days
- the prerequisite checks (tool versions, .env keys, reachability, device, licences)

Run any script with `python3 -I build/<script>.py`. Nothing here imports third-party code.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "build" / "tasks.yaml"
PROVIDERS = ROOT / "config" / "providers.yaml"
ENV_FILE = ROOT / "backend" / ".env"

STATUSES = ("todo", "claimed", "in_review", "done", "blocked")
TRACKS = ("demo", "later")
DEFAULT_PROFILE = "hackathon"
DEFAULT_TRACK = "demo"
CARD_DEFAULTS = {
    "kind": "core", "slot": None, "impl": None, "always": False, "optional": False, "stretch": False, "phase1": False,
    "track": "later", "estimate_hours": None, "owner_lane": None,
    "depends_on": [], "owns": [], "shared": [], "contracts": [], "human": [], "questions": [],
    "grain_needs": [], "estimate_days": 1, "status": "todo", "claimed_by": None, "issue": None, "pr": None,
}


# ----------------------------------------------------------------------------- YAML subset parser

class YamlSubsetError(Exception):
    pass


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _split_flow(s: str) -> list[str]:
    items, cur, quote = [], [], None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            cur.append(ch)
        elif ch == ",":
            items.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        items.append(tail)
    return [i for i in items if i != ""]


def _scalar(tok: str):
    tok = tok.strip()
    if tok == "" or tok in ("null", "~"):
        return None
    if tok.startswith("[") and tok.endswith("]"):
        return [_scalar(t) for t in _split_flow(tok[1:-1])]
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
        return tok[1:-1]
    if tok == "true":
        return True
    if tok == "false":
        return False
    if re.fullmatch(r"-?\d+", tok):
        return int(tok)
    if re.fullmatch(r"-?\d+\.\d*", tok):
        return float(tok)
    return tok


def _split_kv(text: str):
    """Split `key: value` at the first ': ' (or trailing ':') outside quotes."""
    quote = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == ":" and (i + 1 == len(text) or text[i + 1] == " "):
            return text[:i].strip(), text[i + 1:].strip()
    return None


def parse_yaml_subset(text: str):
    lines = []
    for raw in text.splitlines():
        s = _strip_comment(raw)
        if s.strip() == "":
            continue
        lines.append((len(s) - len(s.lstrip(" ")), s.strip()))

    def parse_block(i: int, indent: int):
        if i >= len(lines):
            return None, i
        if lines[i][1].startswith("- "):
            return parse_list(i, indent)
        return parse_map(i, indent)

    def parse_map(i: int, indent: int):
        out = {}
        while i < len(lines):
            ind, s = lines[i]
            if ind < indent:
                break
            if ind > indent:
                raise YamlSubsetError(f"unexpected indent at line: {s!r}")
            if s.startswith("- "):
                break
            kv = _split_kv(s)
            if kv is None:
                raise YamlSubsetError(f"expected `key: value`, got {s!r}")
            key, val = kv
            if val == "":
                if i + 1 < len(lines) and lines[i + 1][0] > indent:
                    out[key], i = parse_block(i + 1, lines[i + 1][0])
                else:
                    out[key] = None
                    i += 1
            else:
                out[key] = _scalar(val)
                i += 1
        return out, i

    def parse_list(i: int, indent: int):
        out = []
        while i < len(lines):
            ind, s = lines[i]
            if ind < indent or not s.startswith("- "):
                break
            if ind > indent:
                raise YamlSubsetError(f"unexpected indent at line: {s!r}")
            rest = s[2:].strip()
            kv = _split_kv(rest)
            if kv is not None and not (rest.startswith("[") or rest.startswith('"') or rest.startswith("'")):
                # mapping item: rewrite this line as the first key at indent+2 and parse a map from here
                lines[i] = (indent + 2, rest)
                item, i = parse_map(i, indent + 2)
                out.append(item)
            else:
                out.append(_scalar(rest))
                i += 1
        return out, i

    value, i = parse_block(0, lines[0][0] if lines else 0)
    if i != len(lines):
        raise YamlSubsetError(f"trailing content at line: {lines[i][1]!r}")
    return value


# ----------------------------------------------------------------------------- loaders

def load_tasks(path: Path = TASKS, merged: bool = True) -> dict:
    """The registry. With `merged`, a card whose id leads a commit subject on main counts as done (see merged_cards)."""
    data = prepare_tasks(parse_yaml_subset(path.read_text(encoding="utf-8")))
    if merged:
        apply_merged(data, merged_cards(set(data["by_id"])))
    return data


_CARD_IN_SUBJECT = re.compile(r"^\s*\[?([A-Z][A-Z0-9]*(?:\.[A-Z0-9]+)+)\]?(?=[\s:\]]|$)")


def card_ids_in_subjects(subjects, ids: set[str]) -> set[str]:
    """Card ids that lead a commit subject (`S2.4 Alignment (#12)`, `[S2.4] ...`, `S2.4: ...`)."""
    out = set()
    for subj in subjects:
        m = _CARD_IN_SUBJECT.match(subj)
        if m and m.group(1) in ids:
            out.add(m.group(1))
    return out


def merged_cards(ids: set[str], root: Path = ROOT) -> set[str]:
    """Cards merged to main: PR titles carry the card id and squash-merge keeps it as the commit subject, so nobody
    edits `status: done` by hand. Reads origin/main if it exists, else HEAD; no git or no repo gives an empty set."""
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             timeout=10)
    except (OSError, subprocess.SubprocessError):
        return set()
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != Path(root).resolve():
        return set()  # not this repo (no git yet, or a parent folder's repo)
    for ref in ("origin/main", "HEAD"):
        try:
            r = subprocess.run(["git", "-C", str(root), "log", "--format=%s", ref], capture_output=True, text=True,
                               timeout=10)
        except (OSError, subprocess.SubprocessError):
            return set()
        if r.returncode == 0:
            return card_ids_in_subjects(r.stdout.splitlines(), ids)
    return set()


def apply_merged(data: dict, merged: set[str]) -> None:
    for cid in merged:
        if cid in data["by_id"]:
            data["by_id"][cid]["status"] = "done"


def prepare_tasks(data: dict) -> dict:
    """Apply card defaults and index the registry. Separate from load_tasks so tests can pass a dict."""
    for k in ("lanes", "streams", "decisions", "human_steps", "slots", "phases", "done_checklist"):
        data[k] = data.get(k) or []
    cards = []
    for c in data.get("cards", []):
        card = dict(CARD_DEFAULTS)
        card["_keys"] = set(c)
        card.update(c)
        for k in ("depends_on", "owns", "shared", "contracts", "human", "questions", "grain_needs"):
            if card[k] is None:
                card[k] = []
        card["stream"] = card_stream(card, data["streams"])
        cards.append(card)
    data["cards"] = cards
    data["by_id"] = {c["id"]: c for c in cards}
    return data


def card_stream(card: dict, streams: list[dict]):
    """The first stream whose `cards` globs match the card id (tasks.yaml `streams:`), or None."""
    for st in streams:
        if any(fnmatch(card["id"], g) for g in st.get("cards") or []):
            return st["id"]
    return None


def decided_questions(data: dict) -> set[str]:
    """Question ids settled for the 24 hours (`decisions:` in tasks.yaml); not blockers."""
    return {d["id"] for d in data.get("decisions", [])}


def lane_names(data: dict) -> dict[str, dict]:
    return {l["id"]: l for l in data.get("lanes", [])}


def validate_tasks(data: dict) -> list[str]:
    errs = []
    ids = set(data["by_id"])
    slot_ids = {s["id"] for s in data.get("slots", [])}
    human_ids = {h["id"] for h in data.get("human_steps", [])}
    phase_ids = {p["id"] for p in data.get("phases", [])}
    lane_ids = {l["id"] for l in data.get("lanes", [])}
    seen = set()
    for c in data["cards"]:
        cid = c["id"]
        if cid in seen:
            errs.append(f"{cid}: duplicate id")
        seen.add(cid)
        if c["phase"] not in phase_ids:
            errs.append(f"{cid}: unknown phase {c['phase']}")
        if c["status"] not in STATUSES:
            errs.append(f"{cid}: bad status {c['status']}")
        if c["kind"] not in ("core", "module"):
            errs.append(f"{cid}: bad kind {c['kind']}")
        if c["kind"] == "module":
            if c["slot"] not in slot_ids:
                errs.append(f"{cid}: unknown slot {c['slot']}")
            if not c["impl"]:
                errs.append(f"{cid}: module card without impl")
        for d in c["depends_on"]:
            if d not in ids:
                errs.append(f"{cid}: unknown dependency {d}")
        for h in c["human"]:
            if h not in human_ids:
                errs.append(f"{cid}: unknown human step {h}")
        if c["status"] in ("claimed", "in_review") and not c["claimed_by"]:
            errs.append(f"{cid}: {c['status']} without claimed_by")
        if "required" in c.get("_keys", ()):
            errs.append(f"{cid}: `required` is gone; use `always: true` (in play whatever is selected) or `optional: true` (extra)")
        if c["phase1"]:
            for d in c["depends_on"]:
                if d in data["by_id"] and not data["by_id"][d]["phase1"]:
                    errs.append(f"{cid}: phase1 card depends on Phase 2 card {d}")
            if c.get("stream") == "grain" or c["id"].startswith("M.GRAIN.") or c["grain_needs"]:
                errs.append(f"{cid}: phase1 card needs Grain (Grain work is Phase 2)")
        if data.get("streams") and c.get("stream") is None:
            errs.append(f"{cid}: matches no stream in `streams:`")
        if c["track"] not in TRACKS:
            errs.append(f"{cid}: bad track {c['track']!r} (demo | later)")
        if c["track"] == "demo":
            h = c["estimate_hours"]
            if isinstance(h, bool) or not isinstance(h, (int, float)) or not (0 < h <= 4):
                errs.append(f"{cid}: demo card needs estimate_hours in (0, 4], got {h!r}")
            if c["owner_lane"] not in lane_ids:
                errs.append(f"{cid}: demo card needs owner_lane in {sorted(lane_ids)}, got {c['owner_lane']!r}")
            for d in c["depends_on"]:
                if d in data["by_id"] and data["by_id"][d]["track"] != "demo":
                    errs.append(f"{cid}: demo card depends on later card {d}")
    # cycles
    state = {}

    def visit(n, stack):
        if state.get(n) == 1:
            errs.append("dependency cycle: " + " -> ".join(stack + [n]))
            return
        if state.get(n) == 2:
            return
        state[n] = 1
        for d in data["by_id"][n]["depends_on"]:
            if d in ids:
                visit(d, stack + [n])
        state[n] = 2

    for cid in ids:
        visit(cid, [])
    for item in data.get("done_checklist", []):
        for r in item.get("requires", []):
            if r not in ids:
                errs.append(f"done_checklist {item['id']}: unknown card {r}")
    return errs


def load_providers(path: Path = PROVIDERS, profile: str | None = None, environ=None) -> dict:
    """Resolve the active profile: `profile` arg (--profile) > env REPAIROS_PROFILE > `profile:` in the file >
    hackathon. Then apply REPAIROS_SLOT_<SLOT>=<impl> overrides. Returns the file data with `slots`, `profile`."""
    data = parse_yaml_subset(path.read_text(encoding="utf-8")) if path.exists() else {}
    return resolve_profile(dict(data or {}), profile, environ)


def resolve_profile(data: dict, profile: str | None = None, environ=None) -> dict:
    environ = os.environ if environ is None else environ
    profiles = data.get("profiles") or {}
    name = profile or environ.get("REPAIROS_PROFILE") or data.get("profile") or DEFAULT_PROFILE
    if profiles:
        if name not in profiles:
            raise ValueError(f"unknown provider profile {name!r}; config/providers.yaml has: {', '.join(sorted(profiles))}")
        slots = dict(profiles[name] or {})
    else:  # a file without profiles: one flat `slots:` map
        slots = dict(data.get("slots") or {})
        name = "slots"
    for k, v in environ.items():
        if k.startswith("REPAIROS_SLOT_"):
            slots[k[len("REPAIROS_SLOT_"):]] = v
    data["slots"] = slots
    data["profile"] = name
    return data


def selected_impl(providers: dict, slot: str):
    return providers["slots"].get(slot)


def card_is_selected(card: dict, providers: dict) -> bool:
    """A core card or a slot interface is always in play. A module impl card is in play only when its impl is the
    one selected in the active profile, or when it is marked `always` (the Grain replay double for CI).
    An unselected implementation never contributes prerequisites."""
    if card["kind"] == "core" or card["impl"] == "interface":
        return True
    return card["impl"] == selected_impl(providers, card["slot"]) or bool(card.get("always"))


def in_track(card: dict, track: str) -> bool:
    return track == "all" or card["track"] == track


def in_scope(card: dict, providers: dict, track: str) -> bool:
    """In play for the active profile and on this track."""
    return card_is_selected(card, providers) and in_track(card, track)


def counts_toward_done(card: dict) -> bool:
    """Optional extras and stretch items are tracked separately; they never keep the build unfinished."""
    return not card.get("optional") and not card.get("stretch")


def card_cost(card: dict, track: str) -> float:
    """Remaining effort: hours on the demo track, agent-days otherwise. Done cards cost nothing."""
    if card["status"] == "done":
        return 0.0
    if track == "demo":
        return float(card["estimate_hours"] or 0)
    return float(card["estimate_days"] or 0)


def critical_path(data: dict, providers: dict, track: str) -> tuple[float, list[str]]:
    """Longest chain (by card_cost) through not-done, in-scope cards that count toward done.
    Returns (total, [card ids, first to last])."""
    by_id = data["by_id"]
    nodes = {c["id"] for c in data["cards"]
             if in_scope(c, providers, track) and counts_toward_done(c) and c["status"] != "done"}
    dependants: dict[str, list[str]] = {n: [] for n in nodes}
    for n in nodes:
        for d in by_id[n]["depends_on"]:
            if d in nodes:
                dependants[d].append(n)
    memo: dict[str, tuple[float, str | None]] = {}

    def longest(n):
        if n not in memo:
            best, nxt = 0.0, None
            for d in sorted(dependants[n]):
                w = longest(d)[0]
                if w > best:
                    best, nxt = w, d
            memo[n] = (card_cost(by_id[n], track) + best, nxt)
        return memo[n]

    if not nodes:
        return 0.0, []
    start = max(sorted(nodes), key=lambda n: longest(n)[0])
    chain, cur = [], start
    while cur:
        chain.append(cur)
        cur = longest(cur)[1]
    return longest(start)[0], chain


def downstream_weight(data: dict, providers: dict, track: str):
    """card id -> own cost + the longest not-done downstream chain (in scope). Used to sort the ready list."""
    by_id = data["by_id"]
    dependants: dict[str, list[str]] = {c["id"]: [] for c in data["cards"]}
    for c in data["cards"]:
        for d in c["depends_on"]:
            if d in dependants:
                dependants[d].append(c["id"])
    memo: dict[str, float] = {}

    def weight(cid):
        if cid not in memo:
            memo[cid] = card_cost(by_id[cid], track) + max(
                (weight(d) for d in dependants[cid]
                 if in_scope(by_id[d], providers, track) and counts_toward_done(by_id[d])), default=0.0)
        return memo[cid]
    return weight


def required_human_steps(data: dict, providers: dict, only_card_ids=None, track: str = "all") -> dict[str, set[str]]:
    """human step id -> set of card ids (in play for the profile, on the track, not done) that need it."""
    need: dict[str, set[str]] = {}
    for c in data["cards"]:
        if only_card_ids is not None and c["id"] not in only_card_ids:
            continue
        if c["status"] == "done":
            continue
        if not in_scope(c, providers, track):
            continue
        for h in c["human"]:
            need.setdefault(h, set()).add(c["id"])
    # conditional steps tied to config values rather than cards
    st = providers["slots"]
    if st.get("STATE") == "spacetimedb" and st.get("STATE_TARGET") == "maincloud":
        need.setdefault("spacetime_login", set()).add("M.STATE.1")
    else:
        need.pop("spacetime_login", None)
    return need


# ----------------------------------------------------------------------------- glob overlap

def _literal_prefix(glob: str) -> str:
    out = []
    for ch in glob:
        if ch in "*?[":
            break
        out.append(ch)
    return "".join(out)


def globs_overlap(a: str, b: str) -> bool:
    if fnmatch(a, b) or fnmatch(b, a):
        return True
    pa, pb = _literal_prefix(a), _literal_prefix(b)
    return pa.startswith(pb) or pb.startswith(pa)


def owns_overlap(card: dict, other: dict) -> list[tuple[str, str]]:
    hits = []
    for g1 in card["owns"]:
        for g2 in other["owns"]:
            if globs_overlap(g1, g2):
                hits.append((g1, g2))
    return hits


# ----------------------------------------------------------------------------- checks

def read_env(path: Path = ENV_FILE) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():
        env.setdefault(k, v)
    return env


def _run(cmd: list[str], timeout: float = 20) -> tuple[int, str]:
    if shutil.which(cmd[0]) is None:
        return 127, f"{cmd[0]}: not found"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as e:
        return 1, str(e)


def _http_ok(url: str, timeout: float = 5) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "repairos-doctor"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        # any HTTP answer means the host is reachable; 401/404 are fine for a reachability check
        return True, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


def _env_check(env: dict, keys: list[str]) -> tuple[bool, str]:
    missing = [k for k in keys if not env.get(k)]
    if missing:
        return False, "missing in backend/.env: " + ", ".join(missing)
    return True, "set: " + ", ".join(keys) + " (values not shown)"


def run_checks(needed: set[str], offline: bool = False, env: dict | None = None, root: Path | None = None) -> dict[str, tuple]:
    """Return check id -> (ok: True|False|None, detail). None = could not determine."""
    env = env if env is not None else read_env()
    root = root or ROOT
    res: dict[str, tuple] = {}

    def want(cid):
        return cid in needed

    if want("tools_core"):
        parts, ok = [], True
        v = sys.version_info
        parts.append(f"python {v.major}.{v.minor}.{v.micro}")
        if (v.major, v.minor) < (3, 12):
            ok = False
        for tool, args, minv in (("git", ["--version"], None), ("gh", ["--version"], None), ("node", ["--version"], 22)):
            rc, out = _run([tool] + args)
            first = out.splitlines()[0] if out else ""
            if rc != 0:
                ok = False
                parts.append(f"{tool}: MISSING")
            else:
                parts.append(f"{tool}: {first[:40]}")
                if minv:
                    m = re.search(r"v?(\d+)", first)
                    if m and int(m.group(1)) < minv:
                        ok = False
                        parts[-1] += f" (< {minv})"
        res["tools_core"] = (ok, "; ".join(parts))
    if want("xcode"):
        parts, ok = [], True
        for tool, args in (("xcodebuild", ["-version"]), ("swift", ["--version"])):
            rc, out = _run([tool] + args)
            first = out.splitlines()[0] if out else ""
            if rc != 0 or "requires Xcode" in out or "not a developer" in out:
                ok = False
                parts.append(f"{tool}: MISSING or Xcode not installed")
            else:
                parts.append(f"{tool}: {first[:50]}")
        res["xcode"] = (ok, "; ".join(parts))
    if want("gh_auth"):
        rc, out = _run(["gh", "auth", "status"])
        res["gh_auth"] = (rc == 0, out.splitlines()[0][:80] if out else "gh not found")
    if want("env_grain"):
        ok, d = _env_check(env, ["GRAIN_URL", "GRAIN_KEY"])
        if ok and not offline:
            reach, detail = _http_ok(env["GRAIN_URL"].rstrip("/") + "/health")
            d += f"; reachability: {detail}"
            ok = ok and reach
        elif ok:
            d += "; reachability skipped (--offline)"
        res["env_grain"] = (ok, d)
    if want("env_grain_project"):
        res["env_grain_project"] = _env_check(env, ["GRAIN_TEST_PROJECT"])
    if want("env_jev"):
        ok, d = _env_check(env, ["TYPESAFE_KEY"])
        if ok and not offline:
            reach, detail = _http_ok("https://api.typesafe.ai/")
            d += f"; api.typesafe.ai: {detail}"
            ok = ok and reach
        res["env_jev"] = (ok, d)
    if want("env_anthropic"):
        res["env_anthropic"] = _env_check(env, ["ANTHROPIC_API_KEY"])
    if want("env_openai"):
        res["env_openai"] = _env_check(env, ["OPENAI_API_KEY"])
    if want("env_openai_compatible"):
        res["env_openai_compatible"] = _env_check(env, ["OPENAI_COMPATIBLE_URL"])
    if want("env_runpod"):
        res["env_runpod"] = _env_check(env, ["RUNPOD_API_KEY"])
    if want("modal_cli"):
        rc, out = _run(["modal", "token", "show"]) if shutil.which("modal") else (127, "modal CLI not installed")
        res["modal_cli"] = (rc == 0, out.splitlines()[0][:80] if out else "")
    if want("env_lambda"):
        res["env_lambda"] = _env_check(env, ["LAMBDA_API_KEY"])
    if want("spacetime_cli"):
        rc, out = _run(["spacetime", "--version"])
        res["spacetime_cli"] = (rc == 0, out.splitlines()[0][:80] if out else "spacetime CLI not found")
    if want("spacetime_server"):
        ok, d = _env_check(env, ["SPACETIMEDB_URL", "SPACETIMEDB_DB"])
        if ok and not offline:
            reach, detail = _http_ok(env["SPACETIMEDB_URL"].rstrip("/") + "/v1/ping")
            d += f"; server: {detail}"
            ok = ok and reach
        elif ok:
            d += "; reachability skipped (--offline)"
        res["spacetime_server"] = (ok, d)
    if want("spacetime_login"):
        # `spacetime login show` prints the logged-in identity (CLI 2.x); if the subcommand differs, result is None
        rc, out = _run(["spacetime", "login", "show"])
        res["spacetime_login"] = (True if rc == 0 else None, (out.splitlines()[0][:80] if out else "could not determine"))
    if want("env_s3"):
        res["env_s3"] = _env_check(env, ["S3_ENDPOINT", "S3_BUCKET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"])
    if want("env_nexar"):
        res["env_nexar"] = _env_check(env, ["NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET"])
    if want("env_digikey"):
        res["env_digikey"] = _env_check(env, ["DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"])
    if want("env_host_ssh"):
        res["env_host_ssh"] = _env_check(env, ["HOST_SSH"])
    if want("device"):
        if sys.platform != "darwin":
            res["device"] = (None, "not a Mac; cannot check for a connected iPhone")
        else:
            rc, out = _run(["xcrun", "devicectl", "list", "devices"], timeout=40)
            if rc != 0:
                res["device"] = (None, "xcrun devicectl failed: " + (out.splitlines()[0][:60] if out else ""))
            else:
                iphones = [l for l in out.splitlines() if "iPhone" in l]
                pro = [l for l in iphones if "16 Pro" in l or "iPhone17," in l]
                if pro:
                    res["device"] = (True, f"{len(pro)} iPhone 16 Pro-class device(s) listed")
                elif iphones:
                    res["device"] = (None, f"{len(iphones)} iPhone(s) listed; could not confirm a 16 Pro / Pro Max")
                else:
                    res["device"] = (False, "no iPhone listed by devicectl")
    if want("signing"):
        xc = root / "ios" / "Config" / "Signing.xcconfig"
        if xc.exists() and "DEVELOPMENT_TEAM" in xc.read_text(encoding="utf-8"):
            res["signing"] = (True, "ios/Config/Signing.xcconfig sets DEVELOPMENT_TEAM")
        else:
            res["signing"] = (False, "ios/Config/Signing.xcconfig with DEVELOPMENT_TEAM not found")
    def files_check(paths, note=""):
        have = [p for p in paths if (root / p).exists()]
        miss = [p for p in paths if not (root / p).exists()]
        parts = (["present: " + ", ".join(have)] if have else []) + (["missing: " + ", ".join(miss)] if miss else [])
        return (not miss, "; ".join(parts) + note)

    # B0.5's prerequisite is the base licence files (B0.1's output) plus the owner's decisions, NOT the manifest
    # B0.5 itself writes. The finished manifest is `license_manifest`, the release check (O9.4).
    if want("license_base"):
        res["license_base"] = files_check(("LICENSE", "NOTICE"), "; owner decisions Q18/Q-S1/Q-S2 are manual")
    if want("license_manifest"):
        res["license_manifest"] = files_check(("LICENSE", "NOTICE", "licenses/manifest.json"))
    if want("none"):
        res["none"] = (None, "manual step; confirm with the human")
    return res


def human_step_satisfied(step: dict, checks: dict) -> tuple:
    cid = step.get("check", "none")
    if cid not in checks:
        return None, "not checked"
    return checks[cid]


def gh_issue_claims() -> dict[str, dict]:
    """card id -> {number, assignees, title} from open GitHub issues labelled `card`. Empty if gh is unavailable."""
    if shutil.which("gh") is None:
        return {}
    rc, out = _run(["gh", "issue", "list", "--label", "card", "--state", "open", "--limit", "500",
                    "--json", "number,title,assignees"], timeout=30)
    if rc != 0 or not out:
        return {}
    try:
        items = json.loads(out)
    except json.JSONDecodeError:
        return {}
    claims = {}
    for it in items:
        m = re.match(r"\s*([A-Z]+\d*\.[A-Za-z0-9]+(?:\.\d+)?)\b", it.get("title", ""))
        if m:
            claims[m.group(1)] = {"number": it["number"], "assignees": [a.get("login") for a in it.get("assignees", [])],
                                  "title": it["title"]}
    return claims
