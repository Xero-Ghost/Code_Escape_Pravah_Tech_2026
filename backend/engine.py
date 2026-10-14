import hashlib
import json
from pathlib import Path
import random
import time
from mongo_store import repo
from models import Settings, Team, Chamber, Question, Match, Audit
# Enrich only unchanged bundled questions, including questions already stored in Mongo.
QUESTION_ENRICHMENTS = json.loads(Path(__file__).with_name('question_enrichments.json').read_text())

KINDS = ['case', 'arrange', 'error', 'quiz']

def require(condition, message, status=400):
    if not condition:
        raise ArenaError(message, status)

class ArenaError(Exception):

    def __init__(self, message, status=400):
        self.message, self.status = (message, status)

def audit(action, team_id=None, detail=''):
    repo.add(Audit(at=time.time(), team_id=team_id, action=action, detail=str(detail)[:500]))

def question_json(q, admin=False):
    fingerprint = hashlib.sha256(json.dumps({k: getattr(q, k, '') for k in ['kind', 'title', 'prompt', 'choices', 'answer', 'explanation', 'source']}, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    enrichment = QUESTION_ENRICHMENTS.get(fingerprint, {})
    result = dict(id=q.id, kind=q.kind, title=q.title, prompt=enrichment.get('prompt', q.prompt), choices=q.choices, hint=getattr(q, 'hint', '') or enrichment.get('hint', ''), year_level=getattr(q, 'year_level', '') or '')
    if admin:
        result.update(answer=q.answer, explanation=q.explanation, source=q.source)
    return result

def validate_question(q):
    require(isinstance(q, dict), 'Each question must be an object')
    require(q.get('kind') in KINDS, 'kind must be case, arrange, error or quiz')
    require(isinstance(q.get('title'), str) and 0 < len(q['title']) <= 200, 'A title (1â€“200 characters) is required')
    require(isinstance(q.get('prompt'), str) and 0 < len(q['prompt']) <= 10000, 'A prompt is required (maximum 10,000 characters)')
    c = q.get('choices')
    require(isinstance(c, list) and 2 <= len(c) <= 20 and all((isinstance(x, str) and 0 < len(x) <= 2000 for x in c)), 'choices must contain 2â€“20 non-empty strings')
    require(len(set(c)) == len(c), 'Choices / pseudocode lines must be unique')
    require(isinstance(q.get('explanation', ''), str) and len(q.get('explanation', '')) <= 10000, 'Explanation must be text (maximum 10,000 characters)')
    require(isinstance(q.get('hint', ''), str) and len(q.get('hint', '')) <= 2000, 'Hint must be text (maximum 2,000 characters)')
    source = q.get('source', '')
    require(isinstance(source, str) and len(source) <= 1000 and (not source or source.startswith(('https://', 'http://'))), 'Source must be an http or https URL')
    a = q.get('answer')
    if q['kind'] == 'arrange':
        require(isinstance(a, list) and all((type(x) is int for x in a)) and (sorted(a) == list(range(len(c)))), 'Arrangement answer must be a permutation of zero-based line indexes')
    else:
        require(type(a) is int and 0 <= a < len(c), 'Answer must be a zero-based choice index')
    year_level = q.get('year_level', '')
    require(year_level in ['', '1st Year', '2nd Year'], 'year_level must be 1st Year or 2nd Year')
    return {k: q.get(k, '') for k in ['kind', 'title', 'prompt', 'choices', 'answer', 'explanation', 'source', 'hint']} | {'year_level': year_level}

def expire_team(team, settings, now=None):
    now = now or time.time()
    if team.status not in ['registered', 'playing']:
        return
    starts = getattr(team, 'member_started_at', {}) or {}
    timed_out = set(getattr(team, 'timed_out_members', []) or [])
    chambers = repo.find(Chamber, {'team_id': {'$eq': team.id}})
    for member_key, member_start in starts.items():
        member = int(member_key)
        if member in timed_out:
            continue
        chamber = next((c for c in chambers if c.member == member), None)
        if chamber and chamber.submitted_at is not None:
            continue
        deadline = member_start + settings.total_seconds
        if chamber:
            deadline = min(deadline, chamber.started_at + settings.chamber_seconds[chamber.slot])
        if now >= deadline:
            timed_out.add(member)
            if chamber:
                chamber.correct = False
                chamber.submitted_at = deadline
            audit('member_timeout', team.id, f'Member {member + 1}')
    team.timed_out_members = sorted(timed_out)
    failed = timed_out | {c.member for c in chambers if c.correct is False}
    if len(failed) >= 2:
        team.status = 'eliminated'
        team.reason = 'Two team members answered incorrectly or ran out of time'
    elif team.status == 'registered' and starts:
        team.status = 'playing'
    if len(chambers) + len(timed_out - {c.member for c in chambers}) >= 4 and all(c.submitted_at is not None for c in chambers) and sum(c.correct is True for c in chambers) >= 3 and len(failed) < 2:
        team.status = 'qualified'
        team.reason = 'Stage 1 completed with at least three correct answers'

def match_json(m):
    return {k: getattr(m, k) for k in ['id', 'round', 'position', 'label', 'a', 'b', 'started_at', 'winner', 'status']} | {'participant_members': getattr(m, 'participant_members', {}) or {}, 'scores': {k: {'score': sum((x['correct'] for x in v.values())), 'time': round(sum((x['elapsed'] for x in v.values())), 2), 'answered': len(v)} for k, v in (m.answers or {}).items()}}

def questions_for_match():
    ids = [q.id for q in repo.find(Question, {}) if q.title.startswith('Round 2 · ')]
    require(len(ids) >= 16, 'Round 2 advanced question set is not ready')
    return random.SystemRandom().sample(ids, 16)

def make_round(team_ids, round_no, label=None):
    count = len(team_ids)
    for i in range(0, count, 2):
        a = team_ids[i]
        b = team_ids[i + 1] if i + 1 < count else None
        title = label or ('Final' if count == 2 else 'Semifinal' if count == 4 else 'Knockout')
        repo.add(Match(round=round_no, position=i // 2, label=title, a=a, b=b, question_ids=questions_for_match(), answers={}, winner=a if b is None else None, status='complete' if b is None else 'ready'))
    repo.flush()

def advance_bracket():
    repo.lock()
    matches = repo.find(Match, {}, sort=[('round', 1), ('position', 1)])
    ordinary = [m for m in matches if m.label != 'Third place']
    if not ordinary:
        return
    last = max((m.round for m in ordinary))
    current = [m for m in ordinary if m.round == last]
    if any((m.status != 'complete' for m in current)) or len(current) == 1:
        return
    winners = [m.winner for m in current]
    if len(current) == 2:
        losers = [m.b if m.winner == m.a else m.a for m in current]
        if all(losers):
            make_round(losers, last + 1, 'Third place')
    make_round(winners, last + 1)

def settle_match(m, settings):
    if m.status != 'live':
        return
    answers = m.answers or {}
    starts = getattr(m, 'member_started_at', {}) or {}
    complete = True
    for team_id in [m.a, m.b]:
        member = (getattr(m, 'participant_members', {}) or {}).get(str(team_id))
        started = starts.get(str(team_id), {}).get(str(member))
        entries = answers.get(str(team_id), {})
        if member is None or started is None or (len(entries) < 16 and time.time() < started + settings.match_seconds):
            complete = False
    if not complete:
        return
    repo.lock()
    stats = []
    for team_id in [m.a, m.b]:
        entries = answers.get(str(team_id), {})
        stats.append((sum(bool(x['correct']) for x in entries.values()),
                      max((x['elapsed'] for x in entries.values()), default=0) + max(0, 16-len(entries))*settings.match_seconds))
    if stats[0] == stats[1]:
        m.status = 'tie'
        audit('exact_tie', detail=f'Match {m.id}: coordinator decision required')
    else:
        m.winner = m.a if (-stats[0][0], stats[0][1]) < (-stats[1][0], stats[1][1]) else m.b
        m.status = 'complete'
        audit('match_complete', detail=f'Match {m.id}, winner {m.winner}')
        advance_bracket()
