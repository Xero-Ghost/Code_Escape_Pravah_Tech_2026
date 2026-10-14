"""Isolated route regressions: never connect to the event database."""
import ast
import json
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]

class ArenaTests(unittest.TestCase):
    def route(self, name, payload, objects, overrides=None):
        tree = ast.parse((ROOT / 'backend/app.py').read_text())
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        fn.decorator_list = []
        class Repo:
            def get(self, model, key): return objects.get((model, key))
            def find(self, model, query): return [v for (k, _), v in objects.items() if k == model]
            def delete(self, value):
                for key in list(objects):
                    if objects[key] is value: del objects[key]
            def commit(self): pass
        def require(condition, message, status=400):
            if not condition: raise ValueError(message)
        env = dict(repo=Repo(), Team='team', Match='match', settings=lambda *a: NS(), body=lambda:payload,
                   require=require, jsonify=lambda **kw:kw, audit=lambda *a, **kw:None, advance_bracket=lambda:None)
        env.update(overrides or {})
        exec(compile(ast.Module(body=[fn], type_ignores=[]), '<route>', 'exec'), env)
        return env[name]

    def test_bye_can_return_to_pool(self):
        objects={('team',1):NS(status='qualified'), ('match',1):NS(a=1,b=None,round=1,status='complete',id=1)}
        self.route('unseed_team', {}, objects)(1)
        self.assertNotIn(('match',1),objects)
        self.assertEqual(objects['team',1].status,'qualified')

    def test_move_up_without_reason(self):
        m=NS(a=1,b=2,round=1,status='ready',winner=None)
        objects={('team',1):NS(status='qualified'),('match',1):m}
        self.route('manually_advance_match',{'winner':1},objects)(1)
        self.assertEqual((m.status,m.winner),('complete',1))

    def test_repeated_move_preserves_later_round(self):
        m=NS(a=1,b=2,round=1,status='complete',winner=1)
        objects={('team',1):NS(status='qualified'),('match',1):m,('match',2):NS(round=2,status='ready')}
        self.route('manually_advance_match',{'winner':1},objects)(1)
        self.assertIn(('match',2),objects)

    def test_move_back_without_reason(self):
        m=NS(id=1,a=1,b=2,round=1,status='complete',winner=1)
        objects={('match',1):m,('match',2):NS(a=1,b=3,round=2,status='ready')}
        self.route('rollback_match',{'team_id':1},objects)(1)
        self.assertEqual(m.status,'ready')
        self.assertNotIn(('match',2),objects)

    def test_stage_change(self):
        settings=NS(stage='lobby',valid_from=0,valid_until=100)
        self.route('update_settings',{'stage':'stage2'},{}, {'settings':lambda *a:settings})()
        self.assertEqual(settings.stage,'stage2')

    def test_restore_returns_to_stage1_without_granting_passes(self):
        for has_chambers in [False, True]:
            team=NS(status='disqualified',reason='Violation',started_at=1)
            pending=NS(submitted_at=None,started_at=1,correct=None)
            completed=NS(submitted_at=2,started_at=1,correct=True)
            chambers=[pending,completed] if has_chambers else []
            repo=NS(one=lambda *a,**k:None,find=lambda *a:chambers,
                    delete_where=lambda *a:None,commit=lambda:None)
            self.route('team_action',{'action':'restore'},{},dict(repo=repo,
                team_locked=lambda _:team,Chamber='chamber',Session='session',
                time=NS(time=lambda:100))) (1)
            self.assertEqual(team.status,'playing' if has_chambers else 'registered')
            self.assertIsNone(team.reason)
            if has_chambers:
                self.assertEqual(pending.started_at,100)
                self.assertIsNone(pending.correct)
                self.assertEqual(completed.submitted_at,2)
                self.assertTrue(completed.correct)

    def test_question_hints_and_paragraphs(self):
        import hashlib
        tree=ast.parse((ROOT/'backend/engine.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='question_json')
        env=dict(hashlib=hashlib,json=json,QUESTION_ENRICHMENTS=json.loads((ROOT/'backend/question_enrichments.json').read_text()))
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<serializer>','exec'),env)
        for q in json.loads((ROOT/'backend/questions.json').read_text()):
            result=env['question_json'](NS(id=1,**q))
            self.assertTrue(result['hint'])
            self.assertNotIn('answer',result)
            if q['kind']=='case':
                self.assertIn('\n\n',result['prompt'])
                self.assertGreater(len(result['prompt'].split()),70)
        custom=NS(id=1,kind='quiz',title='Custom',prompt='Custom prompt',choices=['a','b'],answer=0,explanation='',source='',hint='My hint')
        self.assertEqual(env['question_json'](custom)['hint'],'My hint')
        self.assertEqual(env['question_json'](custom)['prompt'],'Custom prompt')

    def test_every_question_accepts_its_correct_answer(self):
        import time
        for q in json.loads((ROOT/'backend/questions.json').read_text()):
            with self.subTest(title=q['title']):
                team=NS(id=1,status='playing')
                chamber=NS(question_id=1,submitted_at=None,correct=None,slot=0)
                repo=NS(one=lambda *a,**k:chamber, get=lambda *a:NS(**q),
                        flush=lambda:None,find=lambda *a:[chamber],commit=lambda:None)
                env=dict(repo=repo,settings=lambda *a:NS(stage='stage1'),participant=lambda:team,
                         expire_team=lambda *a:None,Chamber='chamber',Question='question',
                         g=NS(session=NS(member=0)),time=time)
                result=self.route('submit',{'answer':q['answer']},{},env)()
                self.assertTrue(result['correct'])

    def test_all_question_schemas_and_answer_keys(self):
        tree=ast.parse((ROOT/'backend/engine.py').read_text())
        nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in ['require','ArenaError','validate_question']]
        env={'KINDS':['case','arrange','error','quiz']}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'<validator>','exec'),env)
        questions=json.loads((ROOT/'backend/questions.json').read_text())
        self.assertEqual(len(questions),60)
        for q in questions:
            with self.subTest(title=q['title']):
                env['validate_question'](q)
                self.assertTrue(q['source'].startswith('https://'))
                self.assertTrue(q['explanation'])
        for kind in env['KINDS']:
            self.assertEqual(sum(q['kind']==kind for q in questions),15)

if __name__=='__main__': unittest.main()
