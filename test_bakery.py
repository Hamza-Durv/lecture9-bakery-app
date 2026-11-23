"""Business-rule checks performed only on a temporary database copy."""
import shutil
import tempfile
import unittest
from pathlib import Path
import app

class BakeryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.original=app.DB
        app.DB=Path(self.tmp.name)/'bakery.db'
        if self.original.exists(): shutil.copy2(self.original,app.DB)
        # Build a pristine scenario on the copy, regardless of saved user decisions.
        with app.connect() as c:
            c.execute('PRAGMA foreign_keys=OFF')
            for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                c.execute('DROP TABLE "'+row[0]+'"')
        app.init()
    def tearDown(self):
        app.DB=self.original
        self.tmp.cleanup()
    def decide(self,c,name,kind='accept'):
        m=c.execute('SELECT * FROM messages WHERE customer=? ORDER BY id LIMIT 1',(name,)).fetchone()
        app.action(c,dict(action=kind,id=m['id']))
    def test_import_and_idempotence(self):
        app.init()
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM orders').fetchone()[0],13)
            self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0],25)
            self.assertEqual(len(app.state(c)['messages']),3)
    def test_notice_and_future(self):
        with app.connect() as c:
            with self.assertRaises(ValueError): self.decide(c,'Okafor')
            with self.assertRaises(ValueError): self.decide(c,'Reyes')
            self.decide(c,'Jasmine')
            self.assertEqual(app.usage(c,'2026-11-25'),3.5)
    def test_capacity_and_helper(self):
        with app.connect() as c:
            c.execute("UPDATE settings SET value='2026-11-26' WHERE key='today'")
            self.decide(c,'Moreau');self.decide(c,'Tan')
            with self.assertRaises(ValueError): self.decide(c,'Singh')
            app.action(c,dict(action='helper',day='2026-11-28'))
            self.decide(c,'Singh')
            self.assertEqual(app.usage(c,'2026-11-28'),9)

            with self.assertRaises(ValueError): app.action(c,dict(action='helper',day='2026-11-28'))
            with self.assertRaises(ValueError): app.action(c,dict(action='helper',day='2026-11-27'))
    def test_changes_and_cancellations(self):
        with app.connect() as c:
            c.execute("UPDATE settings SET value='2026-11-24' WHERE key='today'")
            self.decide(c,'Brown')
            self.assertEqual(app.usage(c,'2026-11-26'),5)
            c.execute("UPDATE settings SET value='2026-11-25' WHERE key='today'")
            self.decide(c,'Lam')
            c.execute("UPDATE settings SET value='2026-11-26' WHERE key='today'")
            m=c.execute("SELECT * FROM messages WHERE customer='Lam' AND kind='change'").fetchone()
            with self.assertRaises(ValueError): app.action(c,dict(action='accept',id=m['id']))
            self.assertEqual(c.execute("SELECT quantity FROM orders WHERE customer='Lam'").fetchone()[0],2)
    def test_forward_requires_decisions(self):
        with app.connect() as c:
            with self.assertRaises(ValueError): app.action(c,dict(action='day',day='2026-11-24'))
            for name in ('Jasmine','Okafor','Patel'): self.decide(c,name,'reject')
            app.action(c,dict(action='day'))
            self.assertEqual(app.today(c),'2026-11-24')
            with self.assertRaises(ValueError): app.action(c,dict(action='day',day='2026-11-23'))

    def test_end_of_week_and_same_day(self):
        with app.connect() as c:
            with self.assertRaises(ValueError): app.action(c,dict(action='day',day=app.today(c)))
            c.execute("UPDATE settings SET value='2026-11-29' WHERE key='today'")
            with self.assertRaises(ValueError): app.action(c,dict(action='day'))
            self.assertEqual(app.today(c),'2026-11-29')

    def test_inbox_values(self):
        with app.connect() as c:
            self.assertEqual(app.state(c)['messages'][0]['order_value'],120)
            c.execute("UPDATE settings SET value='2026-11-24' WHERE key='today'")
            brown=next(m for m in app.state(c)['messages'] if m['customer']=='Brown')
            self.assertEqual((brown['order_value'],brown['revenue_change']),(80,-80))
            self.decide(c,'Brown')
            brown=next(m for m in app.state(c)['messages'] if m['customer']=='Brown')
            self.assertEqual((brown['order_value'],brown['revenue_change']),(80,-80))
            c.execute("UPDATE settings SET value='2026-11-25' WHERE key='today'")
            moreau=next(m for m in app.state(c)['messages'] if m['customer']=='Moreau')
            self.assertEqual((moreau['order_value'],moreau['revenue_change']),(240,80))

    def test_fetch_and_batch_capacity(self):
        with app.connect() as c:
            first=app.state(c)
            jasmine=next(o for d in first['calendar'] for o in d['orders'] if o['customer']=='Jasmine')
            self.assertEqual(jasmine['status'],'requested')
            okafor=next(m for m in first['messages'] if m['customer']=='Okafor')
            self.assertEqual(okafor['status'],'rejected')
            self.assertIn('Incompatible',okafor['note'])
            count=c.execute('SELECT count(*) FROM orders').fetchone()[0]
            app.state(c)
            self.assertEqual(c.execute('SELECT count(*) FROM orders').fetchone()[0],count)
            app.action(c,dict(action='accept_all_next'))
            self.assertEqual(app.today(c),'2026-11-24')
            self.assertEqual(c.execute('SELECT status FROM orders WHERE id=?',(jasmine['id'],)).fetchone()[0],'accepted')
            self.assertEqual(app.usage(c,'2026-11-25'),3.5)
            self.assertEqual(c.execute("SELECT count(*) FROM orders WHERE pickup='2026-11-23' AND status='accepted'").fetchone()[0],0)
            self.assertGreater(c.execute("SELECT count(*) FROM orders WHERE status='picked up'").fetchone()[0],0)
            c.execute("UPDATE settings SET value='2026-11-26' WHERE key='today'")
            # Both fit independently at fetch; the batch must recheck after each.
            c.execute("INSERT INTO messages(day,customer,kind,product,quantity,pickup,raw) VALUES('2026-11-26','Batch A','new','cake',2,'2026-12-01','test')")
            c.execute("INSERT INTO messages(day,customer,kind,product,quantity,pickup,raw) VALUES('2026-11-26','Batch B','new','cake',3,'2026-12-01','test')")
            app.state(c)
            app.action(c,dict(action='accept_all_next'))
            statuses=dict(c.execute("SELECT customer,status FROM orders WHERE customer LIKE 'Batch %'"))
            self.assertEqual(statuses,{'Batch A':'accepted','Batch B':'rejected'})
            self.assertLessEqual(app.usage(c,'2026-12-01'),8)

    def test_helper_cannot_fix_notice_or_more_than_twelve_hours(self):
        with app.connect() as c:
            m=next(m for m in app.state(c)['messages'] if m['customer']=='Okafor')
            self.assertFalse(m['helper_offer'])
            with self.assertRaises(ValueError): app.action(c,dict(action='helper_accept',id=m['id']))
            c.execute("UPDATE settings SET value='2026-11-26' WHERE key='today'")
            c.execute("INSERT INTO orders(customer,product,quantity,pickup,status) VALUES('Full day','cake',6,'2026-11-28','accepted')")
            m=next(m for m in app.state(c)['messages'] if m['customer']=='Singh')
            self.assertFalse(m['helper_offer'])
            with self.assertRaises(ValueError): app.action(c,dict(action='helper_accept',id=m['id']))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM helpers').fetchone()[0],0)

    def test_days_link_orders_and_helpers(self):
        import sqlite3
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM days').fetchone()[0],9)
            self.assertEqual(list(c.execute('PRAGMA foreign_key_check')),[])
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE orders SET pickup='2099-01-01' WHERE id=(SELECT min(id) FROM orders)")
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO helpers VALUES('2099-01-01')")
            result=app.state(c)
            grouped=[o for d in result['calendar'] for o in d['orders']]
            self.assertEqual(len(grouped),16)
            self.assertEqual(len({o['id'] for o in grouped}),16)
            self.assertNotIn('orders',result)
            self.assertTrue(all('pickup' not in o for o in grouped))

    def test_upgrade_preserves_saved_decisions(self):
        with app.connect() as c:
            self.decide(c,'Jasmine')
            app.action(c,dict(action='helper',day='2026-11-28'))
            baseline=app.state(c)
            # Recreate the old unlinked tables on the temporary copy only.
            for table in ('orders','helpers'):
                c.execute(f'CREATE TABLE {table}_old AS SELECT * FROM {table}')
                c.execute(f'DROP TABLE {table}')
                c.execute(f'ALTER TABLE {table}_old RENAME TO {table}')
            c.execute('DROP TABLE days')
        app.init()
        app.init()
        with app.connect() as c:
            self.assertEqual(app.state(c),baseline)
            self.assertEqual(list(c.execute('PRAGMA foreign_key_check')),[])

    def test_reset(self):
        with app.connect() as c:
            baseline=app.state(c)
            self.decide(c,'Jasmine')
            app.action(c,dict(action='helper',day='2026-11-28'))
            with self.assertRaises(ValueError): app.action(c,dict(action='reset'))
            self.assertTrue(app.state(c)['audit'])
            app.action(c,dict(action='reset',confirm='RESET'))
            self.assertEqual(app.state(c),baseline)
            self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0],25)
        app.init()
        with app.connect() as c: self.assertEqual(app.state(c),baseline)

if __name__=='__main__': unittest.main()
