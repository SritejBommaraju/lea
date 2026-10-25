"""Shared helpers for build/doctor.py, build/next.py and build/status.py. Stdlib only.

- a parser for the strict YAML subset used by build/tasks.yaml and config/providers.yaml
- the task registry: defaults, validation, done-from-git, ready tasks, human steps, milestones
- provider profile resolution (same rules as backend/repairos/registry.py)
- the prerequisite checks behind HUMAN ACTION NEEDED (tool versions, backend/.env keys, device, signing)

Run any script with `python3 -I build/<script>.py`. Nothing here imports third-party code.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "build" / "tasks.yaml"
PROVIDERS = ROOT / "config" / "providers.yaml"
ENV_FILE = ROOT / "backend" / ".env"

STATUSES = ("todo", "doing", "done", "blocked")
PHASES = (1, 2)
DEFAULT_PROFILE = "phase1"
TASK_DEFAULTS = {"status": "todo", "phase": 1, "depends_on": [], "owns": [], "human": [], "slots": [],
                 "done_when": "", "branch": None}
LIST_FIELDS = ("depends_on", "owns", "human", "slots")


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


# ----------------------------------------------------------------------------- task registry

def load_tasks(path: Path = TASKS, merged: bool = True) -> dict:
    """The registry. With `merged`, a task whose id leads a commit subject on main counts as done."""
    data = prepare_tasks(parse_yaml_subset(path.read_text(encoding="utf-8")))
    if merged:
        for tid in merged_tasks(set(data["by_id"])):
            data["by_id"][tid]["status"] = "done"
    return data


def prepare_tasks(data: dict) -> dict:
    """Apply task defaults and index the registry. Separate from load_tasks so tests can pass a dict."""
    for k in ("streams", "human_steps", "milestones", "tasks"):
        data[k] = data.get(k) or []
    tasks = []
    for t in data["tasks"]:
        task = {**TASK_DEFAULTS, **t}
        for k in LIST_FIELDS:
            task[k] = list(task[k] or [])
        tasks.append(task)
    data["tasks"] = tasks
    data["by_id"] = {t["id"]: t for t in tasks}
    return data


def validate_tasks(data: dict) -> list[str]:
    errs = []
    by_id = data["by_id"]
    streams = {s["id"] for s in data["streams"]}
    humans = {h["id"] for h in data["human_steps"]}
    seen = set()
    for t in data["tasks"]:
        tid = t["id"]
        if tid in seen:
            errs.append(f"{tid}: duplicate id")
        seen.add(tid)
        if not _TASK_ID.fullmatch(tid):
            errs.append(f"{tid}: id must be letters then a number (A1, M7, B10)")
        if t.get("stream") not in streams:
            errs.append(f"{tid}: unknown stream {t.get('stream')!r}")
        if t["phase"] not in PHASES:
            errs.append(f"{tid}: phase must be 1 or 2, got {t['phase']!r}")
        if t["status"] not in STATUSES:
            errs.append(f"{tid}: bad status {t['status']!r} ({' | '.join(STATUSES)})")
        if not t.get("title") or not t["done_when"] or not t["owns"]:
            errs.append(f"{tid}: needs title, owns and done_when")
        for d in t["depends_on"]:
            if d not in by_id:
                errs.append(f"{tid}: unknown dependency {d}")
            elif t["phase"] == 1 and by_id[d]["phase"] != 1:
                errs.append(f"{tid}: Phase 1 task depends on Phase 2 task {d}")
            elif t["status"] == "done" and by_id[d]["status"] != "done":
                errs.append(f"{tid}: done but its dependency {d} is not")
        for h in t["human"]:
            if h not in humans:
                errs.append(f"{tid}: unknown human step {h}")
        if t["status"] == "doing" and not t["branch"]:
            errs.append(f"{tid}: doing needs `branch` (where the work lives)")
        for s in t["slots"]:
            if not re.fullmatch(r"[A-Z]+=[a-z0-9_]+", str(s)):
                errs.append(f"{tid}: slot {s!r} must look like DET=owlv2")
    for m in data["milestones"]:
        for r in m.get("requires") or []:
            if r not in by_id:
                errs.append(f"milestone {m['id']}: unknown task {r}")
    state: dict[str, int] = {}

    def visit(n, stack):
        if state.get(n) == 1:
            errs.append("dependency cycle: " + " -> ".join(stack + [n]))
        elif state.get(n) is None:
            state[n] = 1
            for d in by_id[n]["depends_on"]:
                if d in by_id:
                    visit(d, stack + [n])
            state[n] = 2

    for tid in sorted(by_id):
        visit(tid, [])
    return errs


_TASK_ID = re.compile(r"[A-Z]+\d+")
_ID_IN_SUBJECT = re.compile(r"^\s*\[?([A-Z]+\d+)\]?(?=[\s:\]]|$)")


def ids_in_subjects(subjects, ids: set[str]) -> set[str]:
    """Task ids that lead a commit subject (`B10 Pipeline (#12)`, `[B10] ...`, `B10: ...`)."""
    out = set()
    for subj in subjects:
        m = _ID_IN_SUBJECT.match(subj)
        if m and m.group(1) in ids:
            out.add(m.group(1))
    return out


def merged_tasks(ids: set[str], root: Path = ROOT) -> set[str]:
    """Tasks merged to main (origin/main if it exists, else HEAD). No git or another repo gives an empty set."""
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=10)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != Path(root).resolve():
            return set()
        for ref in ("origin/main", "HEAD"):
            r = subprocess.run(["git", "-C", str(root), "log", "--format=%s", ref], capture_output=True, text=True,
                               timeout=10)
            if r.returncode == 0:
                return ids_in_subjects(r.stdout.splitlines(), ids)
    except (OSError, subprocess.SubprocessError):
        pass
    return set()


def deps_done(task: dict, by_id: dict) -> bool:
    return all(by_id[d]["status"] == "done" for d in task["depends_on"] if d in by_id)


def open_tasks(data: dict, stream: str | None = None) -> list[dict]:
    return [t for t in data["tasks"] if t["status"] != "done" and (stream is None or t["stream"] == stream)]


def required_human_steps(data: dict, task_ids=None, stream: str | None = None) -> dict[str, set[str]]:
    """human step id -> ids of the not-done tasks (optionally limited) that need it."""
    need: dict[str, set[str]] = {}
    for t in open_tasks(data, stream):
        if task_ids is None or t["id"] in task_ids:
            for h in t["human"]:
                need.setdefault(h, set()).add(t["id"])
    return need


def milestone_progress(data: dict, ms: dict) -> tuple[int, int]:
    req = ms.get("requires") or []
    return sum(data["by_id"][r]["status"] == "done" for r in req if r in data["by_id"]), len(req)


# ----------------------------------------------------------------------------- providers

def load_providers(path: Path = PROVIDERS, environ=None) -> dict:
    data = parse_yaml_subset(path.read_text(encoding="utf-8")) if path.exists() else {}
    return resolve_profile(dict(data or {}), environ)


def resolve_profile(data: dict, environ=None) -> dict:
    """profile = REPAIROS_PROFILE > `profile:` > phase1; then REPAIROS_SLOT_<SLOT>=<impl> overrides."""
    environ = os.environ if environ is None else environ
    profiles = data.get("profiles") or {}
    name = environ.get("REPAIROS_PROFILE") or data.get("profile") or DEFAULT_PROFILE
    if name not in profiles:
        raise ValueError(f"unknown provider profile {name!r}; config/providers.yaml has: {', '.join(sorted(profiles))}")
    slots = {k: str(v) for k, v in (profiles[name] or {}).items()}
    for k, v in environ.items():
        if k.startswith("REPAIROS_SLOT_"):
            slots[k[len("REPAIROS_SLOT_"):]] = v
    return {**data, "slots": slots, "profile": name}


# ----------------------------------------------------------------------------- checks

def read_env(path: Path = ENV_FILE) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
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
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "repairos-doctor"}),
                                    timeout=timeout) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return True, f"HTTP {e.code}"  # any HTTP answer means the host is reachable
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


def _env_check(env: dict, keys: list[str]) -> tuple[bool, str]:
    missing = [k for k in keys if not env.get(k)]
    if missing:
        return False, "missing in backend/.env: " + ", ".join(missing)
    return True, "set: " + ", ".join(keys) + " (values not shown)"


def _first(out: str, n: int = 60) -> str:
    return out.splitlines()[0][:n] if out else ""


def run_checks(needed: set[str], offline: bool = False, env: dict | None = None, root: Path | None = None) -> dict:
    """check id -> (ok: True | False | None, detail). None = cannot be checked by a tool; ask the human."""
    env = read_env() if env is None else env
    root = root or ROOT
    res: dict[str, tuple] = {}
    if "tools_core" in needed:
        v = sys.version_info
        ok, parts = (v.major, v.minor) >= (3, 12), [f"python {v.major}.{v.minor}.{v.micro}"]
        for tool, minv in (("git", None), ("gh", None), ("node", 22)):
            rc, out = _run([tool, "--version"])
            if rc != 0:
                ok = False
                parts.append(f"{tool}: MISSING")
                continue
            parts.append(f"{tool}: {_first(out, 40)}")
            m = re.search(r"v?(\d+)", _first(out))
            if minv and m and int(m.group(1)) < minv:
                ok = False
                parts[-1] += f" (< {minv})"
        res["tools_core"] = (ok, "; ".join(parts))
    if "xcode" in needed:
        rc, out = _run(["xcodebuild", "-version"])
        ok = rc == 0 and "requires Xcode" not in out
        res["xcode"] = (ok, _first(out) if ok else "Xcode not installed or not selected")
    if "gh_auth" in needed:
        rc, out = _run(["gh", "auth", "status"])
        res["gh_auth"] = (rc == 0, _first(out, 80) or "gh not found")
    if "env_jev" in needed:
        ok, d = _env_check(env, ["TYPESAFE_KEY"])
        if ok and not offline:
            reach, detail = _http_ok("https://api.typesafe.ai/")
            ok, d = reach, d + f"; api.typesafe.ai: {detail}"
        res["env_jev"] = (ok, d)
    if "env_anthropic" in needed:
        res["env_anthropic"] = _env_check(env, ["ANTHROPIC_API_KEY"])
    if "device" in needed:
        if sys.platform != "darwin":
            res["device"] = (None, "not a Mac; cannot look for a connected iPhone")
        else:
            rc, out = _run(["xcrun", "devicectl", "list", "devices"], timeout=40)
            phones = [line for line in out.splitlines() if "iPhone" in line] if rc == 0 else []
            res["device"] = ((True, f"{len(phones)} iPhone(s) listed; confirm it has LiDAR") if phones else
                             (False if rc == 0 else None, "no iPhone listed by devicectl" if rc == 0 else
                              "xcrun devicectl failed: " + _first(out)))
    if "signing" in needed:
        xc = root / "ios" / "Config" / "Signing.xcconfig"
        ok = xc.exists() and "DEVELOPMENT_TEAM" in xc.read_text(encoding="utf-8")
        res["signing"] = (ok, "ios/Config/Signing.xcconfig " + ("sets DEVELOPMENT_TEAM" if ok else "missing"))
    if "none" in needed:
        res["none"] = (None, "manual step; confirm with the human")
    return res


def human_step_satisfied(step: dict, checks: dict) -> tuple:
    return checks.get(step.get("check", "none"), (None, "not checked"))


def check_steps(data: dict, step_ids, offline: bool = False, env: dict | None = None) -> dict[str, tuple]:
    """human step id -> (ok, detail) for the given step ids."""
    steps = {h["id"]: h for h in data["human_steps"]}
    checks = run_checks({steps[h]["check"] for h in step_ids if h in steps}, offline=offline, env=env)
    return {h: human_step_satisfied(steps[h], checks) if h in steps else (False, "unknown step") for h in step_ids}
