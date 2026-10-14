"""Session lifecycle checks without accessing the event database."""
import ast
from functools import wraps
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

class SessionTests(unittest.TestCase):
    def auth(self, session):
        tree = ast.parse((Path(__file__).resolve().parents[1]/'backend/app.py').read_text())
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'auth')
        team = NS(valid_from=None, valid_until=None)
        def require(condition, message, status=400):
            if not condition: raise ValueError((status, message))
        env = dict(wraps=wraps, require=require, request=NS(headers={'Authorization':'Bearer token'}),
                   digest=lambda x:x, Session='session', Team='team', time=NS(time=lambda:1000),
                   repo=NS(get=lambda model,key:session if model=='session' else team),
                   settings=lambda:NS(valid_from=0,valid_until=9999), g=NS())
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<auth>','exec'),env)
        return env['auth']

    def test_disconnected_session_can_resume_before_real_expiry(self):
        session=NS(expires=2000,seen=100,admin=False,team_id=1)
        self.assertEqual(self.auth(session)()(lambda:'resumed')(),'resumed')
        self.assertEqual(session.seen,1000)

    def test_real_expiry_is_still_enforced(self):
        session=NS(expires=999,seen=999,admin=False,team_id=1)
        with self.assertRaises(ValueError): self.auth(session)()(lambda:None)()

    def test_revoked_token_cannot_resume(self):
        with self.assertRaises(ValueError): self.auth(None)()(lambda:None)()

    def test_participant_cannot_access_admin_routes(self):
        session=NS(expires=2000,seen=999,admin=False,team_id=1)
        with self.assertRaises(ValueError): self.auth(session)(True)(lambda:None)()

if __name__=='__main__': unittest.main()
