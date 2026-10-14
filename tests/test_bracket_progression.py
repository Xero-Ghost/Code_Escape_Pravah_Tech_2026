"""Bracket fixtures are isolated from the production database."""
import ast
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from models import Match


class BracketTests(unittest.TestCase):
    def setUp(self):
        self.matches = []
        owner = self
        class Repo:
            def lock(self): pass
            def flush(self): pass
            def find(self, *args, **kwargs): return owner.matches[:]
            def add(self, match):
                match.id = len(owner.matches) + 1
                owner.matches.append(match)
        tree = ast.parse((ROOT / 'backend/engine.py').read_text())
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ['seeded_pairs', 'advance_bracket']]
        self.env = {'repo': Repo(), 'Match': Match}
        exec(compile(ast.Module(body=functions, type_ignores=[]), '<engine>', 'exec'), self.env)

    def seed(self, count):
        self.matches.clear()
        for position, (a, b) in enumerate(self.env['seeded_pairs'](list(range(1, count + 1)))):
            winner = (a or b) if a is None or b is None else None
            self.env['repo'].add(Match(round=1, position=position, a=a, b=b,
                winner=winner, status='complete' if winner else 'ready', question_ids=[]))
        self.env['advance_bracket']()

    def test_every_draw_has_full_tree_and_correct_byes(self):
        for count in range(2, 65):
            with self.subTest(count=count):
                self.seed(count)
                size = 1 << (count - 1).bit_length()
                self.assertEqual(len(self.matches), size - 1)
                first = [m for m in self.matches if m.round == 1]
                self.assertEqual(sum(m.status == 'complete' for m in first), size - count)
                self.assertEqual(sorted(t for m in first for t in (m.a, m.b) if t is not None), list(range(1, count + 1)))
                self.assertTrue(all(m.status != 'complete' for m in self.matches if m.round > 1))

    def test_five_seeds_distribute_byes_and_wait_for_real_winner(self):
        self.seed(5)
        self.assertEqual([(m.a, m.b) for m in self.matches if m.round == 1], [(1, None), (4, 5), (2, None), (3, None)])
        semi = [m for m in self.matches if m.round == 2]
        self.assertEqual([(m.a, m.b, m.status) for m in semi], [(1, None, 'waiting'), (2, 3, 'ready')])
        match = next(m for m in self.matches if m.a == 4)
        match.winner, match.status = 5, 'complete'
        self.env['advance_bracket']()
        self.assertEqual((semi[0].a, semi[0].b, semi[0].status), (1, 5, 'ready'))

    def test_two_semifinal_winners_populate_final_and_recovery_is_idempotent(self):
        self.seed(4)
        for m in self.matches[:2]: m.winner, m.status = m.a, 'complete'
        self.env['advance_bracket']()
        final = next(m for m in self.matches if m.label == 'Final')
        self.assertEqual((final.a, final.b, final.status), (1, 2, 'ready'))
        self.assertEqual(len([m for m in self.matches if m.label == 'Third place']), 1)
        count = len(self.matches)
        self.env['advance_bracket']()
        self.assertEqual(len(self.matches), count)


if __name__ == '__main__': unittest.main()
