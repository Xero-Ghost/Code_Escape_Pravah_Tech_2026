import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from models import Question

class Collection:
    def __init__(self): self.counter = 0; self.reservations = 0; self.inserts = []; self.writes = []
    def find_one_and_update(self, query, update, **kw):
        self.reservations += 1
        self.counter += update['$inc']['value']
        return {'value': self.counter}
    def insert_many(self, docs, **kw): self.inserts.append(deepcopy(docs))
    def bulk_write(self, operations, **kw): self.writes.append(operations)

class Database:
    def __init__(self): self.counters = Collection(); self.collections = {}
    def __getitem__(self, name): return self.collections.setdefault(name, Collection())

class BatchTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / 'backend/mongo_store.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Repository')
        env = {'deepcopy':deepcopy, 'ReturnDocument':NS(AFTER=True),
               'ReplaceOne':lambda query, doc:('replace',query,doc),
               'DeleteOne':lambda query:('delete',query), 'BulkWriteError':RuntimeError}
        exec(compile(ast.Module(body=[cls],type_ignores=[]), '<repository>', 'exec'), env)
        self.db = Database(); self.repo = env['Repository'](self.db, None)

    def test_question_import_reserves_ids_once_and_inserts_once(self):
        questions = [Question(title=str(i)) for i in range(46)]
        self.repo.add_many(questions); self.repo.flush()
        self.assertEqual(self.db.counters.reservations, 1)
        self.assertEqual([q.id for q in questions], list(range(1,47)))
        self.assertEqual(len(self.db['question'].inserts), 1)
        self.repo.flush()
        self.assertEqual(len(self.db['question'].inserts), 1)

    def test_updates_and_deletion_are_batched_and_not_repeated(self):
        qs = [Question(title=str(i)) for i in range(10)]
        self.repo.add_many(qs); self.repo.flush()
        for q in qs: q.difficulty = 'Hard'
        self.repo.delete(qs[-1]); self.repo.flush()
        writes = self.db['question'].writes
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(writes[0]), 10)
        self.assertEqual(sum(op[0] == 'delete' for op in writes[0]), 1)
        self.repo.flush()
        self.assertEqual(len(writes), 1)
        self.assertIsNone(self.repo.get(Question, qs[-1].id))

if __name__ == '__main__': unittest.main()
