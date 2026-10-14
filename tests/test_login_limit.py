import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

class LoginLimitTests(unittest.TestCase):
    def setUp(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'backend/mongo_store.py').read_text())
        methods=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name in
                 ['login_bucket','rate_limit','record_login_failure','reset_login_failures']]
        scope={'time':NS(time=lambda:1000),'datetime':datetime,'timezone':timezone}
        exec(compile(ast.Module(body=methods,type_ignores=[]),'<limits>','exec'),scope)
        self.rows={}
        def update(query, change, **kwargs):
            row=self.rows.setdefault(query['_id'],{'count':0})
            row['count']+=change['$inc']['count']
        collection=NS(find_one=lambda q:self.rows.get(q['_id']),update_one=update,
                      delete_one=lambda q:self.rows.pop(q['_id'],None))
        cls=type('Limiter',(),{n.name:scope[n.name] for n in methods})
        self.limiter=cls();self.limiter.database=NS(login_attempt=collection)

    def test_normal_login_checks_do_not_consume_limit(self):
        for _ in range(100):self.assertTrue(self.limiter.rate_limit('team'))
        self.assertFalse(self.rows)

    def test_incorrect_passwords_are_limited_and_success_resets(self):
        for _ in range(15):self.limiter.record_login_failure('team')
        self.assertFalse(self.limiter.rate_limit('team'))
        self.limiter.reset_login_failures('team')
        self.assertTrue(self.limiter.rate_limit('team'))

    def test_legacy_request_counter_does_not_block_login(self):
        self.rows['team:3']={'count':100}
        self.assertTrue(self.limiter.rate_limit('team'))

if __name__=='__main__':unittest.main()
