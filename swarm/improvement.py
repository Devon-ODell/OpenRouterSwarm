"""Durable, bounded discovery state. No model calls or task execution here."""
import hashlib
import json
from pathlib import PurePosixPath
import re
import time


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def write(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n')
    temp.replace(path)


def paths(value):
    """Use explicit files or directory prefixes, never unrestricted/glob scopes."""
    if not isinstance(value, list) or not value or len(value) > 30:
        raise ValueError('Declare 1–30 allowed files or directory prefixes')
    for p in value:
        if (not isinstance(p, str) or not p.strip() or p.startswith('/')
                or any(x in p for x in ('*', '?', '[', '\\', '\x00'))
                or any(part in ('..', '.git', '.env') for part in PurePosixPath(p).parts)
                or str(PurePosixPath(p)) == '.'):
            raise ValueError('Allowed paths must be concrete repository-relative paths')
    return list(dict.fromkeys(value))


def contains(path, scope):
    return any(path == p.rstrip('/') or path.startswith(p.rstrip('/') + '/') for p in scope)


def candidates(raw, scope, baseline):
    """Reject unsupported proposals; evidence remains a claim until the gates run."""
    if not isinstance(raw, list) or len(raw) > 3:
        raise ValueError('Discovery must return at most three candidates')
    result = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        required = ('title', 'problem', 'evidence', 'benefit', 'verification', 'area')
        if any(not isinstance(row.get(k), str) or not row[k].strip() for k in required):
            continue
        allowed = paths(row.get('allowed_paths'))
        if not all(contains(p.rstrip('/'), scope) for p in allowed):
            continue
        acceptance = row.get('acceptance')
        if (not isinstance(acceptance, list) or not 1 <= len(acceptance) <= 12
                or any(not isinstance(x, str) or not x.strip() or len(x) > 2000 for x in acceptance)):
            continue
        effort = row.get('effort_minutes')
        if type(effort) not in (int, float) or not 0 < effort <= 20:
            continue
        normalized = re.sub(r'\W+', ' ', row['problem'].lower()).strip()
        key = hashlib.sha256((normalized + '|' + row['area'].strip().lower()).encode()).hexdigest()[:20]
        evidence_key = hashlib.sha256(row['evidence'].strip().encode()).hexdigest()[:20]
        rank = lambda k: row.get(k) if type(row.get(k)) is int and 1 <= row[k] <= 5 else 1
        result.append({**{k: row[k][:4000] for k in required}, 'id': key,
                       'evidence_key': evidence_key, 'allowed_paths': allowed,
                       'acceptance': acceptance, 'effort_minutes': effort,
                       'score': rank('impact') * rank('confidence') / effort,
                       'baseline_commit': baseline, 'created': time.time(), 'disposition': 'proposed'})
    return sorted(result, key=lambda p: -p['score'])


PROMPT = """Inspect the accepted repository snapshot, its goals, AGENTS.md and recent changes.
Propose at most THREE small improvements; [] is correct when evidence is weak.
Do not edit, invent measurements, expand the roadmap, or create planning subtasks.
Prefer reproduced bugs and observable user problems over generic refactoring.
Respect repository ownership and approval gates. Financial software changes never
authorize live account actions. Each proposal must fit one 20-minute attempt.
Performance work needs a comparable before/after measurement. Graphics work needs
paired captures and interaction checks, separating human design judgment. Features
need a demonstrated flow and regression checks. Preserve existing acceptance targets.

GOALS AND CONSTRAINTS:
{goal}
ALLOWED SCOPE: {scope}
RECENT TASKS / PREVIOUS PROPOSALS (do not repeat rejected ideas without new evidence):
{history}
Return ONLY a JSON array. Every candidate needs:
{{"title":"bounded outcome", "problem":"specific observable problem",
"area":"affected subsystem", "evidence":"file/line, reproduction or explicit hypothesis",
"benefit":"user benefit", "allowed_paths":["file/or/directory/"],
"verification":"reproducible check and expected outcome; remaining human judgment",
"acceptance":["specific measurable outcome"], "effort_minutes":15,
"impact":3, "confidence":3}}
"""
