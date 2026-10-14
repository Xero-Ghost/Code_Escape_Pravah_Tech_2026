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
from engine import require, ArenaError, KINDS, ROUND2_QUESTION_COUNT, audit, question_json, validate_question, expire_team, match_json, questions_for_match, make_round, seeded_pairs, settle_match, advance_bracket
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
                require(session and session.expires > now, 'Session expired. Please log in again.', 401)
                require(not admin or session.admin, 'Admin access required', 403)
                if not session.admin:
                    team = repo.get(Team, session.team_id)
                    s = settings()
                    require(team is not None, 'Team no longer exists', 401)
                    require((team.valid_from if team.valid_from is not None else s.valid_from) <= now < (team.valid_until if team.valid_until is not None else s.valid_until), 'Team credentials are outside their validity window', 403)
                if now - (session.seen or 0) >= 30:
                    session.seen = now
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
        username = ''.join(email.split()).casefold()
        key = digest(('admin' if admin else username) + ':' + str(request.remote_addr))
        if admin:
            require(app.config['ADMIN_PASSWORD_HASH'] and check_password_hash(app.config['ADMIN_PASSWORD_HASH'], password), 'Invalid admin password', 401)
            team = None
            member = None
        else:
            if '@' in email:
                team = repo.one(Team, {'email': {'$eq': email}})
            else:
                team = repo.one(Team, {'username': {'$eq': username}})
                if team is None:
                    # Existing records from before username indexing can still sign in.
                    candidates = [t for t in repo.find(Team, {}) if t.members and
                                  ''.join(t.members[0]['name'].split()).casefold() == username]
                    require(len(candidates) <= 1, 'This leader name is shared by multiple teams. Ask the organizer to update the team names.', 409)
                    team = candidates[0] if candidates else None
                    if team is not None:
                        team.username = username
            require(team, 'Invalid leader name/email or password', 401)
            leader = team.members[0]
            default_password = initial_password(leader['name'], leader['roll'])
            legacy_default = ''.join(leader['name'].split()) + default_password
            # Recognize only the old generated password; preserve custom hashes.
            if check_password_hash(team.password_hash, legacy_default):
                require(password == default_password, 'Invalid leader name/email or password', 401)
                team.password_hash = generate_password_hash(default_password)
                audit('default_password_updated', team.id, 'Leader roll number default')
                repo.commit()
            else:
                require(check_password_hash(team.password_hash, password), 'Invalid leader name/email or password', 401)
            if b.get('member') is None:
                return jsonify(members=team.members, team=team.name)
            member = b.get('member')
            require(type(member) is int and 0 <= member < 4, 'Choose one of your four members')
            s = settings()
            require((team.valid_from if team.valid_from is not None else s.valid_from) <= now < (team.valid_until if team.valid_until is not None else s.valid_until), 'Credentials are not valid at this time', 403)
            expire_team(team, s, now)
            timed_out = set(getattr(team, 'timed_out_members', []) or [])
            require(member not in timed_out, 'Your Stage 1 time has expired', 409)
            if s.stage == 'stage1' and team.status in ['registered', 'playing']:
                starts = dict(getattr(team, 'member_started_at', {}) or {})
                if str(member) not in starts:
                    starts[str(member)] = now
                    team.member_started_at = starts
                    for chamber in repo.find(Chamber, {'team_id': {'$eq': team.id}}):
                        if chamber.member == member and chamber.submitted_at is None:
                            chamber.started_at = now
                team.status = 'playing'
            active = repo.find(Session, {'$and': [{'team_id': {'$eq': team.id}}, {'expires': {'$gt': now}}, {'seen': {'$gt': now - 90}}]})
            require(len(active) < 4, 'All four team login slots are occupied', 409)
            require(not any((x.member == member for x in active)), 'This member is already logged in. Log out there or wait 90 seconds.', 409)
        if not admin:
            # A new login replaces disconnected sessions for this member.
            repo.delete_where(Session, {'team_id': {'$eq': team.id}, 'member': {'$eq': member}})
        token = secrets.token_urlsafe(40)
        repo.add(Session(token_hash=digest(token), team_id=team.id if team else None, member=member, admin=admin, seen=now, expires=now + settings().session_hours * 3600))
        audit('admin_login' if admin else 'login', team.id if team else None)
        repo.commit()
        return jsonify(token=token, admin=admin)

    @app.post('/api/logout')
    def logout():
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        session = repo.get(Session, digest(token)) if token else None
        if session:
            repo.delete(session)
        repo.commit()
        return jsonify(ok=True)

    @app.get('/api/state')
    @auth()
    def state():
        now = time.time()
        s = settings(True)
        live_filter = {'status': {'$eq': 'live'}}
        if not g.session.admin:
            live_filter['$or'] = [{'a': g.session.team_id}, {'b': g.session.team_id}]
        for m in repo.find(Match, live_filter):
            settle_match(m, s)
        result = {'admin': g.session.admin, 'settings': settings_json(s), 'server_time': now}
        if g.session.admin:
            advanced = json.loads((Path(__file__).parent / 'round2_questions.json').read_text(encoding='utf-8'))
            known_questions = {q.title: q for q in repo.find(Question, {})}
            new_questions = []
            for question in advanced:
                existing = known_questions.get(question['title'])
                if existing:
                    existing.year_level = question['year_level']
                    existing.difficulty = question.get('difficulty', '')
                    existing.topic_area = question.get('topic_area', '')
                else:
                    new_questions.append(Question(**validate_question(question)))
            repo.add_many(new_questions)
            teams = repo.find(Team, {}, sort=[('_id', 1)])
            playing_ids = [t.id for t in teams if t.status == 'playing']
            chamber_groups = {}
            if playing_ids:
                for chamber in repo.find(Chamber, {'team_id': {'$in': playing_ids}}):
                    chamber_groups.setdefault(chamber.team_id, []).append(chamber)
            for t in teams:
                if t.status == 'playing':
                    expire_team(t, s, now, chambers=chamber_groups.get(t.id, []))
            result.update(teams=[team_json(t) for t in teams], questions=[question_json(q, True) for q in repo.find(Question, {})], audit=[{'at': a.at, 'team_id': a.team_id, 'action': a.action, 'detail': a.detail} for a in repo.find(Audit, {}, sort=[('_id', -1)], limit=100)], sessions=[{'team_id': x.team_id, 'member': x.member, 'seen': x.seen} for x in repo.find(Session, {'$and': [{'team_id': {'$ne': None}}, {'seen': {'$gt': now - 90}}, {'expires': {'$gt': now}}]})])
        else:
            team = participant()
            if s.stage == 'stage1' and team.status in ['registered', 'playing'] and g.session.member is not None:
                starts = dict(getattr(team, 'member_started_at', {}) or {})
                starts.setdefault(str(g.session.member), now)
                team.member_started_at = starts
                team.status = 'playing'
            chambers = repo.find(Chamber, {'team_id': {'$eq': team.id}})
            expire_team(team, s, now, chambers=chambers)
            result['member_timed_out'] = g.session.member in set(getattr(team, 'timed_out_members', []) or [])
            member_started = (getattr(team, 'member_started_at', {}) or {}).get(str(g.session.member))
            result['member_deadline'] = member_started + s.total_seconds if member_started else None
            result.update(team=team_json(team), member=g.session.member, chambers=[])
            active = repo.find(Session, {'$and': [{'team_id': {'$eq': team.id}}, {'seen': {'$gt': now - 90}}, {'expires': {'$gt': now}}]})
            result['online_members'] = [x.member for x in active]
            for c in chambers:
                item = {k: getattr(c, k) for k in ['id', 'slot', 'member', 'started_at', 'submitted_at', 'correct']}
                item['deadline'] = min(c.started_at + s.chamber_seconds[c.slot], ((getattr(team, 'member_started_at', {}) or {}).get(str(c.member), now) + s.total_seconds))
                if c.member == g.session.member and team.status == 'playing' and (c.submitted_at is None):
                    item['question'] = question_json(repo.get(Question, c.question_id))
                result['chambers'].append(item)
        match_filter = {} if g.session.admin else {'$or': [{'a': team.id}, {'b': team.id}]}
        all_matches = repo.find(Match, match_filter, sort=[('round', 1), ('position', 1)])
        if g.session.admin:
            result['matches'] = [dict(match_json(m), question_ids=m.question_ids, team_question_ids=getattr(m, 'team_question_ids', {}) or {}) for m in all_matches]
            result['team_names'] = {str(t.id): t.name for t in teams}
        else:
            result['matches'] = [match_json(m) for m in all_matches if team.id in [m.a, m.b]]
            visible_ids = {team.id, *(t for m in result['matches'] for t in [m['a'], m['b']] if t)}
            result['team_names'] = {str(t.id): t.name for t in repo.find(Team, {'_id': {'$in': list(visible_ids)}})}
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
        require(g.session.member not in set(getattr(team, 'timed_out_members', []) or []), 'Your Stage 1 time has expired', 409)
        require(not repo.one(Chamber, {'$and': [{'team_id': {'$eq': team.id}}, {'$or': [{'slot': {'$eq': slot}}, {'member': {'$eq': g.session.member}}]}]}), 'Chamber occupied, or you already own another chamber', 409)
        questions = [q for q in repo.find(Question, {'kind': {'$eq': KINDS[slot]}}) if not q.title.startswith('Round 2 · ')]
        require(questions, 'No question is available for this chamber', 409)
        q = secrets.choice(questions)
        now = time.time()
        starts = dict(getattr(team, 'member_started_at', {}) or {})
        starts.setdefault(str(g.session.member), now)
        team.member_started_at = starts
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
        repo.flush()
        all_c = repo.find(Chamber, {'team_id': {'$eq': team.id}})
        wrong_members = {x.member for x in all_c if x.correct is False}
        if len(wrong_members) >= 2:
            team.status = 'eliminated'
            team.reason = 'Two members answered their chambers incorrectly'
        elif len(all_c) == 4 and all(x.submitted_at is not None for x in all_c):
            team.status = 'qualified'
            team.reason = 'Stage 1 completed with at least three correct answers'
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
                    repo.add(Team(name=t['name'], email=t['email'], username=''.join(t['members'][0]['name'].split()).casefold(), members=t['members'], password_hash=generate_password_hash(t['password'])))
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
        t = Team(name=b['name'], email=b['email'].strip().lower(), username=''.join(b['members'][0]['name'].split()).casefold(), members=b['members'], password_hash=generate_password_hash(password))
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
        credentials_changed = bool(b.get('password')) or ('email' in b and b['email'].strip().lower() != t.email) or ('members' in b and b['members'] != t.members)
        for key in ['name', 'email', 'members']:
            if key in b:
                setattr(t, key, b[key].strip().lower() if key == 'email' else b[key])
        if 'members' in b:
            t.username = ''.join(t.members[0]['name'].split()).casefold()
        for key in ['valid_from', 'valid_until']:
            if key in b:
                require(b[key] is None or (type(b[key]) in [int, float] and 0 <= b[key] <= 4102444800), 'Invalid credential date')
                setattr(t, key, b[key])
        s = settings()
        require((t.valid_from if t.valid_from is not None else s.valid_from) < (t.valid_until if t.valid_until is not None else s.valid_until), 'Invalid team validity window')
        if b.get('password'):
            require(isinstance(b['password'], str) and len(b['password']) >= 8, 'Password must be at least 8 characters')
            t.password_hash = generate_password_hash(b['password'])
        if credentials_changed:
            repo.delete_where(Session, {'team_id': {'$eq': id}})
        audit('team_updated', id)
        repo.commit()
        return jsonify(ok=True)

    @app.delete('/api/admin/teams/<int:id>')
    @auth(True)
    def delete_team(id):
        s = settings(True)
        t = team_locked(id)
        # Remove only this team's references; keep other teams and match records.
        affected = repo.find(Match, {'$or': [{'a': {'$eq': id}}, {'b': {'$eq': id}}, {'winner': {'$eq': id}}]})
        for match in affected:
            if match.a == id:
                match.a = None
            if match.b == id:
                match.b = None
            match.answers = {key: value for key, value in (match.answers or {}).items() if str(key) != str(id)}
            match.participant_members = {key: value for key, value in (match.participant_members or {}).items() if str(key) != str(id)}
            if match.winner == id or match.status != 'complete':
                match.winner = None
                match.status = 'ready'
                match.started_at = None
            audit('match_team_removed', id, f'Match {match.id}; vacant slot requires coordinator placement')
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
        require(action in ['reset', 'reset_password', 'disqualify', 'revoke', 'qualify', 'unqualify', 'restore'], 'Unknown team action')
        if action == 'reset_password':
            leader = t.members[0]
            t.password_hash = generate_password_hash(initial_password(leader['name'], leader['roll']))
        if action == 'reset':
            require(not repo.one(Match, {'$or': [{'a': {'$eq': id}}, {'b': {'$eq': id}}]}, field='id'), 'Cannot reset a team already in the knockout bracket', 409)
            repo.delete_where(Chamber, {'team_id': {'$eq': id}})
            t.status = 'registered'
            t.reason = None
            t.started_at = None
            t.member_started_at = {}
            t.timed_out_members = []
        if action == 'disqualify':
            require(bool(b.get('reason')), 'A coordinator reason is required')
            t.status = 'disqualified'
            t.reason = str(b['reason'])[:300]
            for m in repo.find(Match, {'$and': [{'status': {'$in': ['ready', 'live', 'tie']}}, {'$or': [{'a': {'$eq': id}}, {'b': {'$eq': id}}]}]}):
                m.winner = m.b if m.a == id else m.a
                m.status = 'complete'
            advance_bracket()
        if action == 'qualify':
            require(t.status != 'qualified', 'This team is already qualified', 409)
            team_matches = [m for m in repo.find(Match, {}) if id in [m.a, m.b]]
            if t.status == 'disqualified' and team_matches:
                prior = [m for m in team_matches if m.status == 'complete' and m.winner != id and m.a is not None]
                require(prior, 'Could not find the knockout match to replay; remove the team from the round tree first', 409)
                source = max(prior, key=lambda m: m.round)
                downstream = [m for m in repo.find(Match, {}) if m.round > source.round]
                require(not any(m.status == 'live' for m in downstream), 'Finish live later-round matches before re-qualifying this team', 409)
                for later in downstream:
                    repo.delete(later)
                source.status = 'ready'
                source.winner = None
                source.started_at = None
                source.answers = {}
                source.participant_members = {}
                source.member_started_at = {}
                source.team_question_ids = {}
                source.question_ids = []
                audit('bracket_requalify', id, f'Reopened match {source.id}; cleared {len(downstream)} downstream matches')
                advance_bracket()
            else:
                require(not team_matches, 'Move this team out of the round tree before qualifying it', 409)
            t.status = 'qualified'
            t.reason = 'Re-qualified by organizer' if b.get('requalify') else 'Stage 1 manually cleared'
        if action == 'unqualify':
            in_bracket = repo.one(Match, {'$or': [{'a': {'$eq': id}}, {'b': {'$eq': id}}]}, field='id')
            require(t.status == 'qualified' and not in_bracket, 'Move the team out of the round tree before returning it to Stage 1', 409)
            repo.delete_where(Chamber, {'team_id': {'$eq': id}})
            t.status = 'registered'
            t.reason = None
            t.started_at = None
        if action == 'restore':
            require(t.status == 'disqualified', 'Only a disqualified team can be restored', 409)
            require(not repo.one(Match, {'$or': [{'a': {'$eq': id}}, {'b': {'$eq': id}}]}, field='id'),
                    'Move the team back from its knockout round before restoring it', 409)
            chambers = repo.find(Chamber, {'team_id': {'$eq': id}})
            now = time.time()
            t.status = 'playing' if chambers else 'registered'
            t.reason = None
            t.started_at = now if chambers else None
            t.member_started_at = {}
            t.timed_out_members = []
            for chamber in chambers:
                if chamber.submitted_at is None:
                    chamber.started_at = now
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
        require(not repo.one(Chamber, {'question_id': {'$eq': id}}, field='id') and (not any((id in (m.question_ids or []) for m in repo.find(Match, {})))), 'Question is assigned to a chamber or match; it cannot be changed', 409)

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
        existing = repo.find(Match, {})
        if existing:
            require(b.get('reseed') is True, 'The bracket is already generated', 409)
            require(not any(m.status == 'live' for m in existing), 'Finish all live matches before reseeding', 409)
            require(not any(m.started_at is not None or m.answers for m in existing),
                    'Played matches cannot be discarded; finish the current bracket first', 409)
            for m in existing:
                repo.delete(m)
        advanced = json.loads((Path(__file__).parent / 'round2_questions.json').read_text(encoding='utf-8'))
        known_questions = {q.title: q for q in repo.find(Question, {})}
        new_questions = []
        for question in advanced:
            existing_question = known_questions.get(question['title'])
            if existing_question:
                existing_question.year_level = question.get('year_level', '')
                existing_question.difficulty = question.get('difficulty', '')
                existing_question.topic_area = question.get('topic_area', '')
            else:
                new_questions.append(Question(**validate_question(question)))
        repo.add_many(new_questions)
        ids = b.get('team_ids')
        qualified = list(repo.find(Team, {'status': {'$eq': 'qualified'}}, field='id'))
        require(isinstance(ids, list) and all((type(x) is int for x in ids)) and (len(ids) >= 2) and (len(set(ids)) == len(ids)) and (set(ids) == set(qualified)), 'Include every qualified team exactly once, in your chosen seed order')
        size = 1
        while size < len(ids):
            size *= 2
        for i, (a, bid) in enumerate(seeded_pairs(ids)):
            winner = (a if a is not None else bid) if a is None or bid is None else None
            repo.add(Match(round=1, position=i, label='Final' if size == 2 else 'Semifinal' if size == 4 else 'Knockout', a=a, b=bid, question_ids=[], answers={}, winner=winner, status='complete' if winner is not None else 'ready'))
        advance_bracket()
        audit('bracket_reseeded' if existing else 'bracket_created', detail=ids)
        repo.commit()
        return jsonify(ok=True)

    @app.post('/api/admin/teams/<int:id>/pass-member')
    @auth(True)
    def pass_member(id):
        s = settings(True)
        require(s.stage == 'stage1', 'Open Stage 1 before recording a member pass', 409)
        team = team_locked(id)
        require(team.status in ['registered', 'playing'],
                'Restore this team before recording a Stage 1 pass', 409)
        require(not repo.one(Match, {'$or': [{'a': {'$eq': id}}, {'b': {'$eq': id}}]}, field='id'),
                'A team in the knockout bracket cannot receive a Stage 1 pass', 409)
        b = body()
        member, slot = b.get('member'), b.get('slot')
        require(type(member) is int and 0 <= member < 4 and type(slot) is int and 0 <= slot < 4,
                'Choose a valid member and chamber')
        chambers = repo.find(Chamber, {'team_id': {'$eq': id}})
        existing_member = next((c for c in chambers if c.member == member), None)
        existing_slot = next((c for c in chambers if c.slot == slot), None)
        require(not existing_member or existing_member.slot == slot, 'This member already owns a different chamber', 409)
        require(not existing_slot or existing_slot.member == member, 'This chamber belongs to another member', 409)
        if existing_member:
            require(existing_member.submitted_at is None or existing_member.correct is True,
                    'An incorrect submitted answer cannot be overwritten with an admin pass', 409)
            existing_member.correct = True
            existing_member.submitted_at = existing_member.submitted_at or time.time()
        else:
            now = time.time()
            if team.started_at is None:
                team.started_at = now
            repo.add(Chamber(team_id=id, slot=slot, member=member, question_id=None,
                             started_at=now, submitted_at=now, correct=True))
        chambers = repo.find(Chamber, {'team_id': {'$eq': id}})
        if len(chambers) == 4 and all(c.submitted_at is not None for c in chambers) and sum(c.correct is True for c in chambers) >= 3:
            team.status = 'qualified'
            team.reason = 'Stage 1 completed with at least three correct answers'
        else:
            team.status = 'playing'
        audit('member_admin_pass', id, f"Member {member + 1}, chamber {slot + 1}; no question attempted")
        repo.commit()
        return jsonify(ok=True, status=team.status)

    @app.patch('/api/admin/matches/<int:id>/questions')
    @auth(True)
    def update_match_questions(id):
        settings(True)
        match = repo.get(Match, id)
        require(match, 'Match not found', 404)
        require(match.status == 'ready' and match.started_at is None,
                'Questions can only be changed before the match starts', 409)
        payload = body()
        teams = payload.get('team_question_ids')
        team_ids = [str(match.a), str(match.b)]
        require(match.a is not None and match.b is not None, 'Wait for both teams to reach this match', 409)
        if payload.get('auto_assign') is True:
            first = questions_for_match()
            second = questions_for_match(excluded_ids=first)
            rng = secrets.SystemRandom()
            teams = {team_ids[0]: rng.sample(first, len(first)),
                     team_ids[1]: rng.sample(second, len(second))}
        require(isinstance(teams, dict), 'Choose automatic assignment or provide question sets')
        require(all(isinstance(teams.get(team_id), list) and len(teams[team_id]) == ROUND2_QUESTION_COUNT
                    and all(type(qid) is int for qid in teams[team_id])
                    and len(set(teams[team_id])) == ROUND2_QUESTION_COUNT for team_id in team_ids),
                f'Choose exactly {ROUND2_QUESTION_COUNT} different questions for each team')
        first, second = (set(teams[t]) for t in team_ids)
        require(not first.intersection(second), 'Each team must receive a different question set', 409)
        for team_id in team_ids:
            qs = [repo.get(Question, qid) for qid in teams[team_id]]
            require(all(q and q.title.startswith('Round 2 · ') for q in qs), 'Choose only Round 2 questions', 409)
            require(sum(q.difficulty == 'Easy' for q in qs) >= 4
                    and sum(q.topic_area == 'Current Tech' for q in qs) >= 4
                    and sum(q.difficulty == 'Hard' for q in qs) >= 4
                    and all(sum(q.kind == kind for q in qs) >= 4 for kind in ['case', 'arrange', 'error']),
                    'Each team needs at least 4 Easy, Current Tech, Hard, and each non-quiz format')
        match.team_question_ids = {team_id: teams[team_id] for team_id in team_ids}
        match.question_ids = teams[team_ids[0]] + teams[team_ids[1]]
        audit('match_questions_updated', detail=f'Match {id}: 16 distinct questions per team')
        repo.commit()
        return jsonify(ok=True, team_question_ids=match.team_question_ids)

    @app.post('/api/admin/matches/<int:id>/start')
    @auth(True)
    def start_match(id):
        s = settings(True)
        m = repo.get(Match, id)
        require(m, 'Match not found', 404)
        require(m.status == 'ready' and m.a is not None and m.b is not None, 'Choose a ready match with two teams', 409)
        assignments = getattr(m, 'team_question_ids', {}) or {}
        team_keys = [str(m.a), str(m.b)]
        valid_assignments = all(isinstance(assignments.get(team_id), list)
            and len(assignments[team_id]) == ROUND2_QUESTION_COUNT
            and len(set(assignments[team_id])) == ROUND2_QUESTION_COUNT for team_id in team_keys)
        valid_assignments = valid_assignments and not set(assignments.get(team_keys[0], [])).intersection(assignments.get(team_keys[1], []))
        if not valid_assignments:
            first = questions_for_match()
            second = questions_for_match(excluded_ids=first)
            assignments = {team_keys[0]: first, team_keys[1]: second}
        for team_id in team_keys:
            qs = [repo.get(Question, qid) for qid in assignments[team_id]]
            valid_mix = (all(q and q.title.startswith('Round 2 · ') for q in qs)
                and sum(q.difficulty == 'Easy' for q in qs) >= 4
                and sum(q.topic_area == 'Current Tech' for q in qs) >= 4
                and sum(q.difficulty == 'Hard' for q in qs) >= 4
                and all(sum(q.kind == kind for q in qs) >= 4 for kind in ['case', 'arrange', 'error']))
            if not valid_mix:
                first = questions_for_match()
                second = questions_for_match(excluded_ids=first)
                assignments = {team_keys[0]: first, team_keys[1]: second}
                break
        # Persist one independent shuffle per team when the match starts.
        assignments = {team_id: secrets.SystemRandom().sample(assignments[team_id], len(assignments[team_id]))
                       for team_id in team_keys}
        m.team_question_ids = assignments
        m.question_ids = assignments[team_keys[0]] + assignments[team_keys[1]]
        selected = body().get('members')
        require(isinstance(selected, dict), 'Choose one representative account for each team')
        for team_id in [m.a, m.b]:
            team = repo.get(Team, team_id)
            member = selected.get(str(team_id))
            require(team and type(member) is int and 0 <= member < len(team.members), 'Choose one valid representative for each team')
        m.participant_members = {str(team_id): selected[str(team_id)] for team_id in [m.a, m.b]}
        m.started_at = time.time()
        m.member_started_at = {str(team_id): {str(selected[str(team_id)]): m.started_at} for team_id in [m.a, m.b]}
        m.status = 'live'
        s.stage = 'stage2'
        audit('match_started', detail=f'{id}; members={m.participant_members}')
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
        if not g.session.admin and m.status == 'live':
            selected_member = (getattr(m, 'participant_members', {}) or {}).get(str(g.session.team_id))
            require(g.session.member == selected_member, 'This match is assigned to another team member', 403)
        settle_match(m, s)
        result = match_json(m)
        if m.status == 'live' and s.stage == 'stage2':
            member = g.session.member
            entries = (m.answers or {}).get(str(g.session.team_id), {})
            assigned = (getattr(m, 'team_question_ids', {}) or {}).get(str(g.session.team_id), [])
            result['questions'] = [question_json(repo.get(Question, q)) for q in assigned if str(q) not in entries]
            result['deadline'] = ((getattr(m, 'member_started_at', {}) or {}).get(str(g.session.team_id), {}).get(str(member), m.started_at) + s.match_seconds)
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
        selected_member = (getattr(m, 'participant_members', {}) or {}).get(str(t.id))
        require(g.session.member == selected_member, 'Only the selected representative account can answer this match', 403)
        settle_match(m, s)
        if m.status != 'live':
            repo.commit()
            raise ArenaError('Match has ended or is not live', 409)
        require(s.stage == 'stage2', 'Stage 2 is not open', 409)
        b = body()
        qid = b.get('question_id')
        assigned = (getattr(m, 'team_question_ids', {}) or {}).get(str(t.id), [])
        require(qid in assigned, 'Question is not assigned to your team in this match')
        started = (getattr(m, 'member_started_at', {}) or {}).get(str(t.id), {}).get(str(g.session.member), m.started_at)
        require(time.time() < started + s.match_seconds, 'Your individual match timer has expired', 409)
        answers = dict(m.answers or {})
        entries = dict(answers.get(str(t.id), {}))
        require(str(qid) not in entries, 'You have already submitted this answer', 409)
        require(len(entries) < ROUND2_QUESTION_COUNT, f'All {ROUND2_QUESTION_COUNT} match questions have been answered', 409)
        q = repo.get(Question, qid)
        entries[str(qid)] = {'correct': type(b.get('answer')) is type(q.answer) and b.get('answer') == q.answer, 'elapsed': round(time.time() - started, 3), 'member': g.session.member}
        answers[str(t.id)] = entries
        m.answers = answers
        settle_match(m, s)
        audit('match_answer', t.id, f'Match {id}, question {qid}')
        repo.commit()
        return jsonify(ok=True)

    @app.get('/api/admin/teams/<int:id>/test-preview')
    @auth(True)
    def admin_test_preview(id):
        team = repo.get(Team, id)
        require(team, 'Team not found', 404)
        chambers = repo.find(Chamber, {'team_id': {'$eq': id}})
        matches = [m for m in repo.find(Match, {}) if id in [m.a, m.b]]
        return jsonify(chambers=[{k: getattr(c, k) for k in
            ['slot', 'member', 'question_id', 'submitted_at', 'correct']} for c in chambers],
            matches=[dict(match_json(m), question_ids=(getattr(m, 'team_question_ids', {}) or {}).get(str(id), []),
                answered=list((m.answers or {}).get(str(id), {}))) for m in matches])

    @app.post('/api/admin/teams/<int:id>/apply-practice')
    @auth(True)
    def apply_practice(id):
        s = settings(True)
        team = repo.get(Team, id)
        require(team, 'Team not found', 404)
        b = body()
        require(b.get('confirm') is True, 'Confirm applying practice results to the real test')
        rows = b.get('answers')
        require(isinstance(rows, list) and 1 <= len(rows) <= 4, 'Submit between one and four practice answers')
        now = time.time()
        graded = []
        for row in rows:
            require(isinstance(row, dict), 'Invalid practice answer')
            member, qid, elapsed = row.get('member'), row.get('question_id'), row.get('elapsed')
            require(type(member) is int and 0 <= member < 4, 'Choose a team member')
            require(type(qid) is int, 'Invalid question')
            q = repo.get(Question, qid)
            require(q, 'Question no longer exists', 409)
            require(type(elapsed) in [int, float] and 0 <= elapsed <= 86400, 'Invalid recorded solving time')
            answer = row.get('answer')
            if q.kind == 'arrange':
                require(isinstance(answer, list) and all(type(i) is int for i in answer)
                        and sorted(answer) == list(range(len(q.choices))), 'Invalid line arrangement')
            else:
                require(type(answer) is int and 0 <= answer < len(q.choices), 'Invalid answer choice')
            graded.append((row, q, answer == q.answer))
        if b.get('mode') == 'stage1':
            require(s.stage == 'stage1', 'Open Stage 1 before applying real chamber results', 409)
            require(team.status in ['registered', 'playing'], 'Only a registered or playing team can receive chamber results', 409)
            require(not any(id in [m.a, m.b] for m in repo.find(Match, {})), 'This team is already in the round tree', 409)
            chambers = repo.find(Chamber, {'team_id': {'$eq': id}})
            slots, members = set(), set()
            for row, q, correct in graded:
                slot = row.get('slot')
                require(type(slot) is int and 0 <= slot < 4 and q.kind == KINDS[slot], 'Question does not belong to this chamber')
                require(slot not in slots and row['member'] not in members, 'Each member can submit only one chamber')
                slots.add(slot); members.add(row['member'])
                require(row['elapsed'] <= s.chamber_seconds[slot], 'Practice exceeded the chamber timer; restart practice', 409)
                existing = next((c for c in chambers if c.slot == slot), None)
                require(not any(c.member == row['member'] and c.slot != slot for c in chambers), 'Member already owns a different chamber', 409)
                if existing:
                    require(existing.member == row['member'] and existing.question_id == q.id,
                            'Real chamber assignment differs; reload the preview', 409)
                    require(existing.submitted_at is None, 'Real chamber already submitted; no results were changed', 409)
                    require(now < existing.started_at + s.chamber_seconds[slot], 'Real chamber timer has expired', 409)
            started = min(now - row['elapsed'] for row, _, _ in graded)
            team.started_at = team.started_at if team.started_at is not None else started
            require(now < team.started_at + s.total_seconds, 'Real team timer has expired', 409)
            for row, q, correct in graded:
                c = next((c for c in chambers if c.slot == row['slot']), None)
                if c is None:
                    c = Chamber(team_id=id, slot=row['slot'], member=row['member'], question_id=q.id,
                                started_at=now-row['elapsed'])
                    repo.add(c); chambers.append(c)
                c.correct, c.submitted_at = correct, now
            team.status = 'eliminated' if len({c.member for c in chambers if c.correct is False}) >= 2 else 'qualified' if len(chambers) == 4 and all(c.submitted_at is not None for c in chambers) and sum(c.correct is True for c in chambers) >= 3 else 'playing'
            team.reason = 'Two members answered incorrectly (admin-applied practice)' if team.status == 'eliminated' else None
        elif b.get('mode') == 'match':
            require(s.stage == 'stage2' and team.status == 'qualified', 'Open Stage 2 for a qualified team', 409)
            match_id = b.get('match_id')
            require(type(match_id) is int, 'Select a real match')
            m = repo.get(Match, match_id)
            require(m and id in [m.a, m.b], 'Match does not belong to this team', 409)
            require(m.status == 'live' and now < m.started_at + s.match_seconds, 'Real match must be live and within its timer', 409)
            answers = dict(m.answers or {})
            entries = dict(answers.get(str(id), {}))
            for row, q, correct in graded:
                require(q.id in (getattr(m, 'team_question_ids', {}) or {}).get(str(id), []), 'Question is not assigned to this team in this match', 409)
                require(str(q.id) not in entries, 'This question already has a real answer; no results were changed', 409)
                require(row['elapsed'] <= s.match_seconds, 'Practice exceeded the match timer', 409)
                entries[str(q.id)] = dict(correct=correct, elapsed=round(row['elapsed'], 3), member=row['member'])
            answers[str(id)] = entries
            m.answers = answers
            settle_match(m, s)
        else:
            raise ArenaError('Choose Stage 1 or a real match')
        audit('practice_applied', id, f"Admin applied {len(graded)} answers; mode={b['mode']}; " +
              '; '.join(f"member={row['member']+1}, question={q.id}, correct={correct}" for row, q, correct in graded))
        repo.commit()
        return jsonify(ok=True, correct=sum(correct for _, _, correct in graded), total=len(graded))

    @app.post('/api/admin/matches/<int:id>/advance')
    @auth(True)
    def manually_advance_match(id):
        settings(True)
        m = repo.get(Match, id)
        require(m, 'Match not found', 404)
        b = body()
        require(m.status in ['ready', 'tie', 'complete'], 'Only a ready, tied or completed match can be manually advanced', 409)
        winner = b.get('winner')
        require(type(winner) is int and winner in [m.a, m.b], 'Choose one of the two teams in this match')
        reason = str(b.get('reason', '')).strip() or 'Coordinator moved team'
        team = repo.get(Team, winner)
        require(team and team.status == 'qualified', 'Restore this team before moving it up', 409)
        if m.status == 'complete' and m.winner == winner:
            return jsonify(ok=True)
        downstream = [later for later in repo.find(Match, {}) if later.round > m.round]
        require(not any(later.status == 'live' for later in downstream), 'Finish or stop live matches before changing an earlier result', 409)
        for later in downstream:
            repo.delete(later)
        m.winner = winner
        m.status = 'complete'
        audit('match_manual_advance', winner, f'Match {id}; cleared {len(downstream)} later-round matches; {reason}')
        advance_bracket()
        repo.commit()
        return jsonify(ok=True)

    @app.post('/api/admin/matches/<int:id>/rollback')
    @auth(True)
    def rollback_match(id):
        settings(True)
        source = repo.get(Match, id)
        require(source, 'Match not found', 404)
        b = body()
        team_id = b.get('team_id')
        reason = str(b.get('reason', '')).strip() or 'Coordinator moved team'
        require(type(team_id) is int and team_id in [source.a, source.b] and source.status == 'complete', 'Drop one of this match’s teams here', 409)
        all_matches = repo.find(Match, {})
        next_round = [m for m in all_matches if m.round == source.round + 1]
        require(any(team_id in [m.a, m.b] for m in next_round), 'That team has not advanced exactly one round from this match', 409)
        downstream = [m for m in all_matches if m.round > source.round]
        require(not any(m.status == 'live' for m in downstream), 'Finish or stop live matches before moving a team back', 409)
        source.winner = None
        source.status = 'ready'
        source.started_at = None
        source.answers = {}
        for m in downstream:
            repo.delete(m)
        audit('bracket_rollback', team_id, f'Match {source.id}; removed {len(downstream)} later-round matches; {reason}')
        repo.commit()
        return jsonify(ok=True, removed_matches=len(downstream), round=source.round)

    @app.post('/api/admin/teams/<int:id>/unseed')
    @auth(True)
    def unseed_team(id):
        settings(True)
        team = repo.get(Team, id)
        require(team and team.status == 'qualified', 'Only a qualified team can move back to the qualified pool', 409)
        matches = repo.find(Match, {})
        source_round = min((m.round for m in matches if id in [m.a, m.b]), default=None)
        require(source_round is not None, 'This team is not in the round tree', 409)
        source = next(m for m in matches if m.round == source_round and id in [m.a, m.b])
        require(source.status != 'live', 'Finish or stop this live match before moving the team down', 409)
        opponent = source.b if source.a == id else source.a
        downstream = [m for m in matches if m.round > source.round]
        require(not any(m.status == 'live' for m in downstream), 'Finish or stop live later-round matches before moving a team down', 409)
        for later in downstream:
            repo.delete(later)
        if opponent is None:
            repo.delete(source)
        source.a, source.b = opponent, None
        source.winner = opponent
        source.status = 'complete'
        source.started_at = None
        source.answers = {}
        audit('bracket_unseed', id, f'Match {source.id}; cleared {len(downstream)} later-round matches')
        advance_bracket()
        repo.commit()
        return jsonify(ok=True, removed_matches=len(downstream), round=source.round)

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
