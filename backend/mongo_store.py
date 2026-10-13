"""MongoDB persistence with retryable transactions and a request-scoped identity map."""
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
import hashlib
import time

from flask import g, jsonify, request, has_request_context
from pymongo import MongoClient, ReturnDocument, timeout
from pymongo.errors import PyMongoError, BulkWriteError, DuplicateKeyError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern
from werkzeug.local import LocalProxy
from werkzeug.security import generate_password_hash as werkzeug_hash
from models import MODELS, Settings

repo = LocalProxy(lambda: g.mongo_repository)


def password_hash(value):
    # Transaction retries must not repeat expensive hashing (especially Excel imports).
    if not has_request_context():
        return werkzeug_hash(value)
    cache = g.setdefault('password_hash_cache', {})
    if value not in cache:
        cache[value] = werkzeug_hash(value)
    return cache[value]


class Repository:
    def __init__(self, database, session):
        self.database, self.session = database, session
        self.objects, self.originals, self.deleted = {}, {}, set()
        self.committed = False
        self.locked = False

    def lock(self):
        if not self.locked:
            result = self.database.control.update_one({'_id':'event'},
                {'$inc':{'revision':1}}, session=self.session)
            if not result.matched_count:
                raise RuntimeError('Run init-db before accepting requests.')
            self.locked = True

    def _track(self, model, document):
        identity = (model.collection, document['_id'])
        if identity not in self.objects:
            data = deepcopy(document)
            data[model.pk] = data.pop('_id')
            self.objects[identity] = model(**data)
            self.originals[identity] = deepcopy(document)
        return self.objects[identity]

    def get(self, model, identity):
        if identity is None:
            return None
        key = (model.collection, identity)
        if key in self.deleted:
            return None
        if key in self.objects:
            return self.objects[key]
        document = self.database[model.collection].find_one({'_id': identity}, session=self.session)
        return self._track(model, document) if document else None

    def find(self, model, where=None, sort=None, limit=None, field=None):
        self.flush()
        projection = {('_id' if field == model.pk else field):1} if field else None
        cursor = self.database[model.collection].find(where or {}, projection, session=self.session)
        if sort:
            cursor = cursor.sort(sort)
        if limit:
            cursor = cursor.limit(limit)
        if field:
            return [document['_id' if field == model.pk else field] for document in cursor]
        return [self._track(model, document) for document in cursor]

    def one(self, model, where=None, sort=None, field=None):
        values = self.find(model, where, sort, limit=1, field=field)
        return values[0] if values else None

    def add(self, obj):
        identity = getattr(obj, obj.pk)
        if identity is None:
            counter = self.database.counters.find_one_and_update(
                {'_id': obj.collection}, {'$inc': {'value': 1}}, upsert=True,
                return_document=ReturnDocument.AFTER, session=self.session)
            identity = counter['value']
            setattr(obj, obj.pk, identity)
        key = (obj.collection, identity)
        if key in self.objects:
            raise ValueError('Duplicate identity in one transaction')
        self.objects[key] = obj
        self.originals[key] = None

    def delete(self, obj):
        self.deleted.add((obj.collection, getattr(obj, obj.pk)))

    def delete_where(self, model, where):
        for obj in self.find(model, where):
            self.delete(obj)
        self.flush()

    def flush(self):
        # Batch new documents to avoid one network round trip per imported row.
        pending = {}
        for key, obj in self.objects.items():
            if key not in self.deleted and self.originals[key] is None:
                pending.setdefault(key[0], []).append((key, obj.document()))
        for collection, entries in pending.items():
            try:
                self.database[collection].insert_many(
                    [document for key, document in entries], session=self.session)
            except BulkWriteError as error:
                if any(item.get('code') == 11000 for item in error.details.get('writeErrors', [])):
                    raise DuplicateKeyError('Duplicate event record', 11000) from None
                raise
            for key, document in entries:
                self.originals[key] = deepcopy(document)
        for key, obj in list(self.objects.items()):
            if key in self.deleted:
                self.database[key[0]].delete_one({'_id': key[1]}, session=self.session)
                del self.objects[key]
                self.originals.pop(key, None)
                continue
            document = obj.document()
            original = self.originals[key]
            if original is None:
                self.database[key[0]].insert_one(document, session=self.session)
            elif document != original:
                self.database[key[0]].replace_one({'_id': key[1]}, document, session=self.session)
            self.originals[key] = deepcopy(document)

    def commit(self):
        # Actual commit is performed by with_transaction after the response is built.
        self.flush()
        self.committed = True


class MongoStore:
    def __init__(self, uri, name):
        if not name or any(c in name for c in '/\\ ."$'):
            raise ValueError('MONGODB_DB must be a valid database name')
        self.client = MongoClient(uri, connect=False, tz_aware=True, maxPoolSize=10, minPoolSize=0,
            maxIdleTimeMS=60000, waitQueueTimeoutMS=5000, serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000, socketTimeoutMS=15000, retryWrites=True) if uri else None
        self.database = self.client[name] if self.client is not None else None

    def ping(self):
        if self.client is None:
            raise RuntimeError('Set MONGODB_URI and run init-db before using the application.')
        self.client.admin.command('ping')

    def initialize(self):
        self.ping()
        info = self.client.admin.command('hello')
        if not info.get('setName') and info.get('msg') != 'isdbgrid':
            raise RuntimeError('MongoDB must support transactions. Use Atlas or a replica set.')
        for model in MODELS:
            # Create collections before transactions; never clear existing records.
            if model.collection not in self.database.list_collection_names():
                self.database.create_collection(model.collection)
            if model.pk == 'id':
                last = self.database[model.collection].find_one(sort=[('_id', -1)])
                self.database.counters.update_one({'_id':model.collection},
                    {'$max':{'value':last['_id'] if last else 0}}, upsert=True)
        self.database.team.create_index('email', unique=True)
        self.database.session.create_index([('team_id', 1), ('expires', 1), ('seen', 1)])
        self.database.session.create_index('purge_at', expireAfterSeconds=0)
        self.database.question.create_index('kind')
        self.database.chamber.create_index([('team_id',1),('slot',1)], unique=True)
        self.database.chamber.create_index([('team_id',1),('member',1)], unique=True)
        self.database.chamber.create_index('question_id')
        self.database.match.create_index([('round',1),('position',1),('label',1)], unique=True)
        self.database.match.create_index('status')
        self.database.login_attempt.create_index('purge_at', expireAfterSeconds=0)
        self.database.control.update_one({'_id':'event'}, {'$setOnInsert':{'revision':0}}, upsert=True)
        self.database.settings.update_one({'_id':1}, {'$setOnInsert':Settings().document()}, upsert=True)

    def rate_limit(self, key):
        now = time.time()
        bucket = int(now // 300)
        row = self.database.login_attempt.find_one_and_update(
            {'_id':f'{key}:{bucket}'}, {'$inc':{'count':1}, '$setOnInsert':{
                'purge_at':datetime.fromtimestamp((bucket+2)*300, timezone.utc)}},
            upsert=True, return_document=ReturnDocument.AFTER)
        return row['count'] <= 15

    def run(self, callback, lock=False, arena_error=None):
        self.ping_if_missing()
        with self.client.start_session() as session:
            def transaction(active):
                g.mongo_repository = Repository(self.database, active)
                if lock:
                    # A shared write prevents write skew between question edits, claims,
                    # credential revocations and bracket generation across Vercel workers.
                    repo.lock()
                try:
                    result = callback()
                except Exception as error:
                    # Explicitly committed timeout/elimination state must survive a 409.
                    if arena_error and isinstance(error, arena_error) and repo.committed:
                        result = (jsonify(error=error.message), error.status)
                    else:
                        raise
                repo.flush()
                return result
            try:
                with timeout(18):
                    return session.with_transaction(transaction, read_concern=ReadConcern('snapshot'),
                        write_concern=WriteConcern('majority'), max_commit_time_ms=10000)
            finally:
                g.pop('mongo_repository', None)

    def ping_if_missing(self):
        if self.client is None:
            self.ping()


def install_transactions(app, store, arena_error):
    @app.errorhandler(PyMongoError)
    def unavailable(error):
        # Never expose connection strings, passwords or document contents.
        app.logger.error('MongoDB operation failed: %s', type(error).__name__)
        return jsonify(error='Database is temporarily unavailable. Please try again.'), 503

    @app.errorhandler(RuntimeError)
    def configuration(error):
        app.logger.error('Database configuration is incomplete')
        return jsonify(error='Database setup is incomplete. Contact the organizer.'), 503

    for endpoint, view in list(app.view_functions.items()):
        if endpoint in ('static', 'health'):
            continue
        def wrap(fn):
            @wraps(fn)
            def wrapped(*args, **kwargs):
                store.ping_if_missing()
                if request.endpoint == 'login':
                    body = request.get_json(silent=True) or {}
                    if not isinstance(body, dict):
                        raise arena_error('A JSON object is required')
                    identity = 'admin' if body.get('admin') is True else str(body.get('email','')).strip().lower()
                    key = hashlib.sha256((identity+':'+str(request.remote_addr)).encode()).hexdigest()
                    if not store.rate_limit(key):
                        raise arena_error('Too many login attempts. Try again in five minutes.',429)
                if request.endpoint == 'import_excel':
                    upload = request.files.get('file')
                    g.import_bytes = upload.read() if upload else b''
                return store.run(lambda: fn(*args, **kwargs),
                    lock=request.method not in ('GET','HEAD','OPTIONS'), arena_error=arena_error)
            return wrapped
        app.view_functions[endpoint] = wrap(view)
