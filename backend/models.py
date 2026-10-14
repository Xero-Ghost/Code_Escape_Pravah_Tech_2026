"""Event documents. All persisted data lives in MongoDB collections."""
from copy import deepcopy
from datetime import datetime, timezone

class Record:
    pk = "id"
    def __init__(self, **values):
        self.__dict__.update(deepcopy(self.defaults))
        self.__dict__.update(values)
    def document(self):
        result = deepcopy(self.__dict__)
        result["_id"] = result.pop(self.pk)
        if self.collection == "session":
            value = datetime.fromtimestamp(self.expires, timezone.utc)
            result["purge_at"] = value.replace(microsecond=(value.microsecond // 1000) * 1000)
        return result

class Settings(Record):
    collection = 'settings'
    pk = 'id'
    defaults = {'id': 1, 'stage': 'lobby', 'total_seconds': 1200, 'chamber_seconds': [600, 600, 600, 600], 'match_seconds': 600, 'valid_from': 0, 'valid_until': 4102444800, 'session_hours': 6, 'strict_fair_play': True}

class Team(Record):
    collection = 'team'
    pk = 'id'
    defaults = {'id': None, 'name': None, 'email': None, 'password_hash': None, 'members': None, 'status': 'registered', 'reason': None, 'started_at': None, 'member_started_at': {}, 'timed_out_members': [], 'valid_from': None, 'valid_until': None}

class Session(Record):
    collection = 'session'
    pk = 'token_hash'
    defaults = {'token_hash': None, 'team_id': None, 'member': None, 'admin': False, 'seen': None, 'expires': None}

class Question(Record):
    collection = 'question'
    pk = 'id'
    defaults = {'id': None, 'kind': None, 'title': None, 'prompt': None, 'choices': None, 'answer': None, 'explanation': '', 'source': '', 'hint': '', 'year_level': '', 'difficulty': '', 'topic_area': ''}

class Chamber(Record):
    collection = 'chamber'
    pk = 'id'
    defaults = {'id': None, 'team_id': None, 'slot': None, 'member': None, 'question_id': None, 'started_at': None, 'submitted_at': None, 'correct': None}

class Match(Record):
    collection = 'match'
    pk = 'id'
    defaults = {'id': None, 'round': None, 'position': None, 'label': 'Knockout', 'a': None, 'b': None, 'question_ids': None, 'team_question_ids': {}, 'answers': {}, 'participant_members': {}, 'member_started_at': {}, 'started_at': None, 'winner': None, 'status': 'ready'}

class Audit(Record):
    collection = 'audit'
    pk = 'id'
    defaults = {'id': None, 'at': None, 'team_id': None, 'action': None, 'detail': ''}

class LoginAttempt(Record):
    collection = 'login_attempt'
    pk = 'key'
    defaults = {'key': None, 'count': 0, 'window': None}

MODELS = [Settings, Team, Session, Question, Chamber, Match, Audit, LoginAttempt]
