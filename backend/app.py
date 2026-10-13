import os, time, secrets, hashlib, json, csv, io
from pathlib import Path
from functools import wraps
from flask import Flask, request, jsonify, Response, g
from flask_cors import CORS
from dotenv import load_dotenv
from werkzeug.security import check_password_hash
from mongo_store import password_hash as generate_password_hash
from pymongo.errors import DuplicateKeyError, PyMongoError
from mongo_store import repo, MongoStore, install_transactions
from models import Settings, Team, Session, Question, Chamber, Match, Audit, LoginAttempt
from engine import require, ArenaError, KINDS, audit, question_json, validate_question, expire_team, match_json, questions_for_match, make_round, settle_match, advance_bracket
from importer import parse_workbook, initial_password
load_dotenv(Path(__file__).with_name('.env'))

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def create_app(config=None):
    app = Flask(__name__)
    app.config.update(MONGODB_URI=os.getenv('MONGODB_URI', ''), MONGODB_DB=os.getenv('MONGODB_DB', 'codeescape'), MAX_CONTENT_LENGTH=4 * 1024 * 1024, ADMIN_PASSWORD_HASH=os.getenv('ADMIN_PASSWORD_HASH', ''))
    if config:
        app.config.update(config)
    if os.getenv('ENVIRONMENT') == 'production' and (not app.config['ADMIN_PASSWORD_HASH']):
        raise RuntimeError('ADMIN_PASSWORD_HASH must be configured')
    store = MongoStore(app.config['MONGODB_URI'], app.config['MONGODB_DB'])
    app.extensions['mongo_store'] = store
    CORS(app, resources={'/api/*': {'origins': os.getenv('FRONTEND_ORIGIN', 'http://127.0.0.1:5173').split(',')}}, allow_headers=['Authorization', 'Content-Type'])

    @app.errorhandler(ArenaError)
    def arena_error(e):
        return (jsonify(error=e.message), e.status)

    @app.errorhandler(DuplicateKeyError)
    def conflict(e):
        return (jsonify(error='This record already exists or is in use. Refresh and try again.'), 409)

    @app.errorhandler(413)
    def too_large(e):
        return (jsonify(error='Upload exceeds the 4 MB limit'), 413)

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def body():
        value = request.get_json(silent=True)
        require(isinstance(value, dict), 'A JSON object is required')
        return value

    def settings(lock=False):
        value = repo.get(Settings, 1)
        require(value is not None, 'Database setup is required. Run init-db.', 503)
        return value

    def team_locked(id):
        team = repo.get(Team, id)
        require(team is not None, 'Team not found', 404)
        return team

    def auth(admin=False):

        def decorate(fn):

            @wraps(fn)
            def wrapped(*args, **kwargs):
                token = request.headers.get('Authorization', '').removeprefix('Bearer ')
                session = repo.get(Session, digest(token)) if token else None
                now = time.time()
                require(session and session.expires > now and (session.admin or session.seen > now - 90), 'Session expired. Please log in again.', 401)
                require(not admin or session.admin, 'Admin access required', 403)
                if not session.admin:
                    team = repo.get(Team, session.team_id)
                    s = settings()
                    require(team is not None, 'Team no longer exists', 401)
                    require((team.valid_from if team.valid_from is not None else s.valid_from) <= now < (team.valid_until if team.valid_until is not None else s.valid_until), 'Team credentials are outside their validity window', 403)
                g.session = session
                return fn(*args, **kwargs)
            return wrapped
        return decorate

    def participant():
        require(not g.session.admin, 'Use a participant login for this action', 403)
        return team_locked(g.session.team_id)

    def team_json(t):
        return {k: getattr(t, k) for k in ['id', 'name', 'email', 'members', 'status', 'reason', 'started_at', 'valid_from', 'valid_until']}

    def settings_json(s):
        return {k: getattr(s, k) for k in ['stage', 'total_seconds', 'chamber_seconds', 'match_seconds', 'valid_from', 'valid_until', 'session_hours', 'strict_fair_play']}

    @app.get('/api/health')
    def health():
        store.ping()
        return jsonify(ok=True, database='mongodb')

    @app.post('/api/login')
    def login():
        b = body()
        now = time.time()
        admin = b.get('admin') is True
        email = str(b.get('email', '')).strip().lower()
        password = str(b.get('password', ''))
        key = digest(('admin' if admin else email) + ':' + str(request.remote_addr))
        if admin:
            require(app.config['ADMIN_PASSWORD_HASH'] and check_password_hash(app.config['ADMIN_PASSWORD_HASH'], password), 'Invalid admin password', 401)
            team = None
            member = None
        else:
            team = repo.one(Team, {'email': {'$eq': email}})
            require(team and check_password_hash(team.password_hash, password), 'Invalid email or password', 401)
            if b.get('member') is None:
                return jsonify(members=team.members, team=team.name)
            member = b.get('member')
            require(type(member) is int and 0 <= member < 4, 'Choose one of your four members')
            s = settings()
            require((team.valid_from if team.valid_from is not None else s.valid_from) <= now < (team.valid_until if team.valid_until is not None else s.valid_until), 'Credentials are not valid at this time', 403)
            active = repo.find(Session, {'$and': [{'team_id': {'$eq': team.id}}, {'expires': {'$gt': now}}, {'seen': {'$gt': now - 90}}]})
            require(len(active) < 4, 'All four team login slots are occupied', 409)
            require(not any((x.member == member for x in active)), 'This member is already logged in. Log out there or wait 90 seconds.', 409)
        token = secrets.token_urlsafe(40)
        repo.add(Session(token_hash=digest(token), team_id=team.id if team else None, member=member, admin=admin, seen=now, expires=now + settings().session_hours * 3600))
        audit('admin_login' if admin else 'login', team.id if team else None)
        repo.commit()
        return jsonify(token=token, admin=admin)

    @app.post('/api/logout')
    @auth()
    def logout():
        repo.delete(g.session)
        repo.commit()
        return jsonify(ok=True)

    @app.get('/api/state')
    @auth()
    def state():
        now = time.time()
        s = settings(True)
        g.session.seen = now
        for m in repo.find(Match, {'status': {'$eq': 'live'}}):
            settle_match(m, s)
        result = {'admin': g.session.admin, 'settings': settings_json(s), 'server_time': now}
        if g.session.admin:
            teams = repo.find(Team, {}, sort=[('_id', 1)])
            for t in teams:
                if t.status == 'playing':
                    expire_team(team_locked(t.id), s, now)
            result.update(teams=[team_json(t) for t in teams], questions=[question_json(q, True) for q in repo.find(Question, {})], audit=[{'at': a.at, 'team_id': a.team_id, 'action': a.action, 'detail': a.detail} for a in repo.find(Audit, {}, sort=[('_id', -1)], limit=100)], sessions=[{'team_id': x.team_id, 'member': x.member, 'seen': x.seen} for x in repo.find(Session, {'$and': [{'team_id': {'$ne': None}}, {'seen': {'$gt': now - 90}}, {'expires': {'$gt': now}}]})])
        else:
            team = participant()
            expire_team(team, s, now)
            chambers = repo.find(Chamber, {'team_id': {'$eq': team.id}})
            result.update(team=team_json(team), member=g.session.member, chambers=[])
            active = repo.find(Session, {'$and': [{'team_id': {'$eq': team.id}}, {'seen': {'$gt': now - 90}}, {'expires': {'$gt': now}}]})
            result['online_members'] = [x.member for x in active]
            for c in chambers:
                item = {k: getattr(c, k) for k in ['id', 'slot', 'member', 'started_at', 'submitted_at', 'correct']}
                item['deadline'] = min(c.started_at + s.chamber_seconds[c.slot], team.started_at + s.total_seconds)
                if c.member == g.session.member and team.status == 'playing' and (c.submitted_at is None):
                    item['question'] = question_json(repo.get(Question, c.question_id))
                result['chambers'].append(item)
        result['matches'] = [match_json(m) for m in repo.find(Match, {}, sort=[('round', 1), ('position', 1)])]
        result['team_names'] = {str(t.id): t.name for t in repo.find(Team, {})}
        repo.commit()
        return jsonify(result)

    @app.post('/api/chambers/claim')
    @auth()
    def claim():
        b = body()
        slot = b.get('slot')
        require(type(slot) is int and 0 <= slot < 4, 'Invalid chamber')
        s = settings(True)
        team = participant()
        expire_team(team, s)
        if team.status == 'eliminated':
            repo.commit()
            raise ArenaError('Your team is eliminated', 409)
        require(s.stage == 'stage1', 'Stage 1 is not open', 409)
        require(team.status in ['registered', 'playing'], 'This team cannot enter Stage 1', 409)
        require(not repo.one(Chamber, {'$and': [{'team_id': {'$eq': team.id}}, {'$or': [{'slot': {'$eq': slot}}, {'member': {'$eq': g.session.member}}]}]}), 'Chamber occupied, or you already own another chamber', 409)
        questions = list(repo.find(Question, {'kind': {'$eq': KINDS[slot]}}))
        require(questions, 'No question is available for this chamber', 409)
        q = secrets.choice(questions)
        now = time.time()
        if team.started_at is None:
            team.started_at = now
        team.status = 'playing'
        repo.add(Chamber(team_id=team.id, slot=slot, member=g.session.member, question_id=q.id, started_at=now))
        audit('chamber_claimed', team.id, f'Member {g.session.member + 1}, chamber {slot + 1}')
        repo.commit()
        return jsonify(ok=True)

    @app.post('/api/chambers/submit')
    @auth()
    def submit():
        b = body()
        s = settings(True)
        team = participant()
        expire_team(team, s)
        if team.status == 'eliminated':
            repo.commit()
            raise ArenaError('Your team has been eliminated', 409)
        require(s.stage == 'stage1' and team.status == 'playing', 'Stage 1 is not active', 409)
        c = repo.one(Chamber, {'$and': [{'team_id': {'$eq': team.id}}, {'member': {'$eq': g.session.member}}]})
        require(c and c.submitted_at is None, 'No unanswered chamber is assigned to you', 409)
        q = repo.get(Question, c.question_id)
        c.correct = type(b.get('answer')) is type(q.answer) and b.get('answer') == q.answer
        c.submitted_at = time.time()
        if not c.correct:
            team.status = 'eliminated'
            team.reason = 'An incorrect chamber answer'
        repo.flush()
        all_c = repo.find(Chamber, {'team_id': {'$eq': team.id}})
        if len(all_c) == 4 and all((x.correct for x in all_c)):
            team.status = 'qualified'
        audit('answer_submitted', team.id, f'Chamber {c.slot + 1}: {c.correct}')
        repo.commit()
        return jsonify(correct=c.correct, status=team.status)

    @app.post('/api/violation')
    @auth()
    def violation():
        s = settings(True)
        team = participant()
        b = body()
        reason = str(b.get('reason', 'Fair play violation'))[:200]
        active_chamber = repo.one(Chamber, {'$and': [{'team_id': {'$eq': team.id}}, {'member': {'$eq': g.session.member}}, {'submitted_at': {'$eq': None}}]})
        live = repo.find(Match, {'$and': [{'status': {'$eq': 'live'}}, {'$or': [{'a': {'$eq': team.id}}, {'b': {'$eq': team.id}}]}]})
        active = s.stage == 'stage1' and team.status == 'playing' and (active_chamber is not None) or bool(live)
        if active:
            audit('fair_play', team.id, reason)
            if s.strict_fair_play:
                team.status = 'disqualified'
                team.reason = reason
                for m in live:
                    m.winner = m.b if m.a == team.id else m.a
                    m.status = 'complete'
                advance_bracket()
        repo.commit()
        return jsonify(ok=True)

    @app.patch('/api/admin/settings')
    @auth(True)
    def update_settings():
        b = body()
        s = settings(True)
        for key in ['total_seconds', 'match_seconds', 'session_hours']:
            if key in b:
                require(type(b[key]) is int and 1 <= b[key] <= 86400, f'{key} must be a positive integer (max 86400)')
                setattr(s, key, b[key])
        if 'chamber_seconds' in b:
            require(isinstance(b['chamber_seconds'], list) and len(b['chamber_seconds']) == 4 and all((type(x) is int and 1 <= x <= 86400 for x in b['chamber_seconds'])), 'Four positive chamber timers are required')
            s.chamber_seconds = b['chamber_seconds']
        for k in ['valid_from', 'valid_until']:
            if k in b:
                require(type(b[k]) in [int, float] and 0 <= b[k] <= 4102444800, 'Invalid credential date')
                setattr(s, k, b[k])
        require(s.valid_from < s.valid_until, 'Validity end must be after its start')
        if 'strict_fair_play' in b:
            require(type(b['strict_fair_play']) is bool, 'strict_fair_play must be boolean')
            s.strict_fair_play = b['strict_fair_play']
        if 'stage' in b:
            require(b['stage'] in ['lobby', 'stage1', 'stage2', 'closed'], 'Invalid event stage')
            if b['stage'] == 'stage1':
                require(all((repo.one(Question, {'kind': {'$eq': k}}, field='id') for k in KINDS)), 'All four question types are required')
            s.stage = b['stage']
        audit('settings_updated', detail=', '.join(b))
        repo.commit()
        return jsonify(ok=True)

    @app.post('/api/admin/import')
    @auth(True)
    def import_excel():
        f = request.files.get('file')
        require(f and f.filename.lower().endswith('.xlsx'), 'Upload an .xlsx workbook')
        try:
            parsed = parse_workbook(g.import_bytes)
        except Exception as e:
            raise ArenaError(f'Cannot read workbook: {str(e)[:150]}')
        existing = set(repo.find(Team, {}, field='email'))
        for t in parsed['teams']:
            t['exists'] = t['email'] in existing
        if request.form.get('commit') == 'true':
            require(not parsed['errors'], 'Correct the reported rows before importing; nothing was imported')
            count = 0
            for t in parsed['teams']:
                if not t['exists']:
                    repo.add(Team(name=t['name'], email=t['email'], members=t['members'], password_hash=generate_password_hash(t['password'])))
                    count += 1
            audit('excel_import', detail=f'{count} teams added; existing emails skipped')
            repo.commit()
            parsed['imported'] = count
        return jsonify(parsed)

    def validate_team(b):
        require(isinstance(b.get('name'), str) and 0 < len(b['name']) <= 120, 'Team name is required')
        import re
        require(isinstance(b.get('email'), str) and len(b['email']) <= 254 and re.fullmatch('[^\\s@]+@[^\\s@]+\\.[^\\s@]+', b['email']), 'Valid leader email required')
        require(isinstance(b.get('members'), list) and len(b['members']) == 4, 'Exactly four members are required')
        require(all((isinstance(m, dict) and isinstance(m.get('name'), str) and isinstance(m.get('roll'), str) and m['name'].strip() and m['roll'].strip() for m in b['members'])), 'Each member needs a name and roll number')
        require(len({m['roll'].strip().lower() for m in b['members']}) == 4, 'Member roll numbers must be unique')

    @app.post('/api/admin/teams')
    @auth(True)
    def add_team():
        b = body()
        validate_team(b)
        password = b.get('password') or initial_password(b['members'][0]['name'], b['members'][0]['roll'])
        require(isinstance(password, str) and len(password) >= 8, 'Password must be at least 8 characters')
        t = Team(name=b['name'], email=b['email'].strip().lower(), members=b['members'], password_hash=generate_password_hash(password))
        repo.add(t)
        audit('team_added', detail=b['name'])
        repo.commit()
        return jsonify(team=team_json(t), password=password)

    @app.patch('/api/admin/teams/<int:id>')
    @auth(True)
    def edit_team(id):
        b = body()
        settings(True)
        t = team_locked(id)
        combined = team_json(t) | b
        validate_team(combined)
        for key in ['name', 'email', 'members']:
            if key in b:
                setattr(t, key, b[key].strip().lower() if key == 'email' else b[key])
        for key in ['valid_from', 'valid_until']:
            if key in b:
                require(b[key] is None or (type(b[key]) in [int, float] and 0 <= b[key] <= 4102444800), 'Invalid credential date')
                setattr(t, key, b[key])
        s = settings()
        require((t.valid_from if t.valid_from is not None else s.valid_from) < (t.valid_until if t.valid_until is not None else s.valid_until), 'Invalid team validity window')
        if b.get('password'):
            require(isinstance(b['password'], str) and len(b['password']) >= 8, 'Password must be at least 8 characters')
            t.password_hash = generate_password_hash(b['password'])
        if any((k in b for k in ['members', 'email', 'password'])):
            repo.delete_where(Session, {'team_id': {'$eq': id}})
        audit('team_updated', id)
        repo.commit()
        return jsonify(ok=True)

    @app.delete('/api/admin/teams/<int:id>')
    @auth(True)
    def delete_team(id):
        s = settings(True)
        t = team_locked(id)
        require(not repo.one(Match, {'$or': [{'a': {'$eq': id}}, {'b': {'$eq': id}}]}, field='id'), 'Team is in the bracket and cannot be deleted', 409)
        repo.delete_where(Session, {'team_id': {'$eq': id}})
        repo.delete_where(Chamber, {'team_id': {'$eq': id}})
        repo.delete(t)
        audit('team_deleted', id)
        repo.commit()
        return jsonify(ok=True)

    @app.post('/api/admin/teams/<int:id>/action')
    @auth(True)
    def team_action(id):
        s = settings(True)
        t = team_locked(id)
        b = body()
        action = b.get('action')
        require(action in ['reset', 'disqualify', 'revoke', 'qualify'], 'Unknown team action')
        if action == 'reset':
            require(not repo.one(Match, {'$or': [{'a': {'$eq': id}}, {'b': {'$eq': id}}]}, field='id'), 'Cannot reset a team already in the knockout bracket', 409)
            repo.delete_where(Chamber, {'team_id': {'$eq': id}})
            t.status = 'registered'
            t.reason = None
            t.started_at = None
        if action == 'disqualify':
            require(bool(b.get('reason')), 'A coordinator reason is required')
            t.status = 'disqualified'
            t.reason = str(b['reason'])[:300]
            for m in repo.find(Match, {'$and': [{'status': {'$in': ['ready', 'live', 'tie']}}, {'$or': [{'a': {'$eq': id}}, {'b': {'$eq': id}}]}]}):
                m.winner = m.b if m.a == id else m.a
                m.status = 'complete'
            advance_bracket()
        if action == 'qualify':
            in_bracket = repo.one(Match, {'$or': [{'a': {'$eq': id}}, {'b': {'$eq': id}}]}, field='id')
            require(not in_bracket, 'A team already placed in the knockout bracket cannot be manually requalified', 409)
            require(t.status != 'qualified', 'This team is already qualified', 409)
            t.status = 'qualified'
            t.reason = 'Manually qualified by organizer'
        if action != 'qualify':
            repo.delete_where(Session, {'team_id': {'$eq': id}})
        audit(action, id, b.get('reason', ''))
        repo.commit()
        return jsonify(ok=True)

    @app.post('/api/admin/questions')
    @auth(True)
    def add_questions():
        b = body()
        entries = b.get('questions', [b])
        require(isinstance(entries, list) and 1 <= len(entries) <= 500, 'Provide 1â€“500 questions')
        validated = [validate_question(q) for q in entries]
        for q in validated:
            repo.add(Question(**q))
        audit('questions_added', detail=len(validated))
        repo.commit()
        return jsonify(added=len(validated))

    def question_unused(id):
        require(not repo.one(Chamber, {'question_id': {'$eq': id}}, field='id') and (not any((id in m.question_ids for m in repo.find(Match, {})))), 'Question is assigned to a chamber or match; it cannot be changed', 409)

    @app.patch('/api/admin/questions/<int:id>')
    @auth(True)
    def edit_question(id):
        settings(True)
        q = repo.get(Question, id)
        require(q, 'Question not found', 404)
        question_unused(id)
        for k, v in validate_question(body()).items():
            setattr(q, k, v)
        repo.commit()
        return jsonify(ok=True)

    @app.delete('/api/admin/questions/<int:id>')
    @auth(True)
    def delete_question(id):
        settings(True)
        q = repo.get(Question, id)
        require(q, 'Question not found', 404)
        question_unused(id)
        repo.delete(q)
        repo.commit()
        return jsonify(ok=True)

    @app.post('/api/admin/bracket')
    @auth(True)
    def bracket():
        s = settings(True)
        b = body()
        require(not repo.one(Match, {}, field='id'), 'The bracket is already generated', 409)
        ids = b.get('team_ids')
        qualified = list(repo.find(Team, {'status': {'$eq': 'qualified'}}, field='id'))
        require(isinstance(ids, list) and all((type(x) is int for x in ids)) and (len(ids) >= 2) and (len(set(ids)) == len(ids)) and (set(ids) == set(qualified)), 'Include every qualified team exactly once, in your chosen seed order')
        size = 1
        while size < len(ids):
            size *= 2
        byes = size - len(ids)
        pairs = []
        for i in range(byes):
            pairs.append((ids[i], None))
        remaining = ids[byes:]
        pairs += [(remaining[i], remaining[i + 1]) for i in range(0, len(remaining), 2)]
        for i, (a, bid) in enumerate(pairs):
            repo.add(Match(round=1, position=i, label='Final' if size == 2 else 'Semifinal' if size == 4 else 'Knockout', a=a, b=bid, question_ids=questions_for_match(), answers={}, winner=a if bid is None else None, status='complete' if bid is None else 'ready'))
        audit('bracket_created', detail=ids)
        repo.commit()
        return jsonify(ok=True)

    @app.post('/api/admin/matches/<int:id>/start')
    @auth(True)
    def start_match(id):
        s = settings(True)
        m = repo.get(Match, id)
        require(m, 'Match not found', 404)
        require(s.stage == 'stage2' and m.status == 'ready', 'Open Stage 2 and select a ready match', 409)
        m.started_at = time.time()
        m.status = 'live'
        audit('match_started', detail=id)
        repo.commit()
        return jsonify(ok=True)

    @app.post('/api/admin/matches/<int:id>/resolve')
    @auth(True)
    def resolve_match(id):
        s = settings(True)
        m = repo.get(Match, id)
        require(m, 'Match not found', 404)
        b = body()
        settle_match(m, s)
        if b.get('winner'):
            require(m.status == 'tie' and b['winner'] in [m.a, m.b] and b.get('reason'), 'Only an exact tie may be resolved, with a reason')
            m.winner = b['winner']
            m.status = 'complete'
            audit('tie_resolved', detail=f"{id}: {b['reason']}")
            advance_bracket()
        repo.commit()
        return jsonify(match=match_json(m))

    @app.get('/api/matches/<int:id>')
    @auth()
    def get_match(id):
        s = settings(True)
        m = repo.get(Match, id)
        require(m, 'Match not found', 404)
        require(g.session.admin or g.session.team_id in [m.a, m.b], 'This match belongs to other teams', 403)
        settle_match(m, s)
        result = match_json(m)
        if m.status == 'live' and s.stage == 'stage2':
            entries = (m.answers or {}).get(str(g.session.team_id), {})
            result['questions'] = [question_json(repo.get(Question, q)) for q in m.question_ids if str(q) not in entries]
            result['deadline'] = m.started_at + s.match_seconds
        repo.commit()
        return jsonify(result)

    @app.post('/api/matches/<int:id>/submit')
    @auth()
    def match_submit(id):
        s = settings(True)
        t = participant()
        m = repo.get(Match, id)
        require(m, 'Match not found', 404)
        require(t.id in [m.a, m.b] and t.status == 'qualified', 'Team cannot answer this match', 403)
        settle_match(m, s)
        if m.status != 'live':
            repo.commit()
            raise ArenaError('Match has ended or is not live', 409)
        require(s.stage == 'stage2', 'Stage 2 is not open', 409)
        b = body()
        qid = b.get('question_id')
        require(qid in m.question_ids, 'Question is not part of this match')
        answers = dict(m.answers or {})
        entries = dict(answers.get(str(t.id), {}))
        require(str(qid) not in entries, 'Your team has already submitted this answer', 409)
        q = repo.get(Question, qid)
        entries[str(qid)] = {'correct': type(b.get('answer')) is type(q.answer) and b.get('answer') == q.answer, 'elapsed': round(time.time() - m.started_at, 3), 'member': g.session.member}
        answers[str(t.id)] = entries
        m.answers = answers
        settle_match(m, s)
        audit('match_answer', t.id, f'Match {id}, question {qid}')
        repo.commit()
        return jsonify(ok=True)

    @app.post('/api/admin/matches/<int:id>/advance')
    @auth(True)
    def manually_advance_match(id):
        settings(True)
        m = repo.get(Match, id)
        require(m, 'Match not found', 404)
        b = body()
        require(m.status in ['ready', 'tie'], 'Only a ready match or an exact tie can be manually advanced', 409)
        winner = b.get('winner')
        require(type(winner) is int and winner in [m.a, m.b], 'Choose one of the two teams in this match')
        reason = str(b.get('reason', '')).strip()
        require(bool(reason), 'A coordinator reason is required')
        m.winner = winner
        m.status = 'complete'
        audit('match_manual_advance', winner, f'Match {id}: {reason}')
        advance_bracket()
        repo.commit()
        return jsonify(ok=True)

    @app.cli.command('init-db')
    def init_db():
        store.initialize()
        print('MongoDB collections and indexes initialized.')

    @app.cli.command('seed-questions')
    def seed_questions():
        data = json.loads((Path(__file__).parent / 'questions.json').read_text(encoding='utf-8'))

        def seed():
            if repo.one(Question):
                print('Question bank already contains entries; no duplicates added.')
                return
            for q in data:
                repo.add(Question(**validate_question(q)))
            repo.commit()
        store.run(seed, lock=True)
        print('Question bank ready.')
    install_transactions(app, store, ArenaError)
    return app
app = create_app()
if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, threaded=True)
