import io
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sandbox = tempfile.TemporaryDirectory()
os.environ['DATA_DIR'] = sandbox.name
os.environ['SECRET_KEY'] = 'test-only-' * 8
os.environ['COOKIE_SECURE'] = '0'
from app import app, init_db, get_db
from werkzeug.security import generate_password_hash

PASSWORD = 'A test password long enough!'

class Workflows(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        Path(sandbox.name, 'disciple_live.db').unlink(missing_ok=True)
        init_db()
        self.mentor = self.account('mentor', 'disciplier')
        self.member = self.account('member', 'disciplee')
        self.other = self.account('other', 'disciplee')
        self.admin = app.test_client()
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO users(name,email,password_hash,role) VALUES (?,?,?,'admin')", ('Admin','admin@example.org',generate_password_hash(PASSWORD)))
            db.commit()
        self.post(self.admin,'/login',email='admin@example.org',password=PASSWORD)

    def post(self, client, url, **data):
        client.get('/login')
        with client.session_transaction() as s:
            data['csrf_token'] = s['csrf']
        return client.post(url, data=data, follow_redirects=True)

    def account(self, name, role):
        client = app.test_client()
        r = self.post(client,'/register',name=name,email=name+'@example.org',password=PASSWORD,role=role,bio='Following Jesus together')
        self.assertEqual(r.status_code,200)
        r = self.post(client,'/login',email=name+'@example.org',password=PASSWORD)
        self.assertEqual(r.status_code,200)
        return client

    def scalar(self, sql):
        with app.app_context():
            return get_db().execute(sql).fetchone()[0]

    def connect(self):
        mentor_id = self.scalar("SELECT id FROM users WHERE role='disciplier'")
        self.post(self.member,'/request-mentor',discipler_id=mentor_id,message='Help me grow')
        req = self.scalar('SELECT id FROM mentor_requests')
        self.post(self.mentor,f'/respond-request/{req}',decision='accepted')
        return mentor_id

    def slot(self, offset=1):
        self.post(self.mentor,'/add-slot',slot_time=(datetime.now(timezone.utc)+timedelta(days=offset)).isoformat())
        return self.scalar('SELECT id FROM availability_slots ORDER BY id DESC')

    def test_complete_member_journey(self):
        self.connect()
        slot = self.slot()
        self.post(self.admin,'/upload-content',title='Abide',description='Read John 15',external_url='https://example.org/study')
        resource = self.scalar('SELECT id FROM content_modules')
        r = self.post(self.member,'/book-session',slot_id=slot,content_module_id=resource)
        self.assertEqual(r.status_code,200)
        self.assertIn(b'Abide',r.data)
        meeting = self.scalar('SELECT id FROM sessions')
        for client in (self.member,self.mentor):
            r = client.get(f'/session/{meeting}')
            self.assertEqual(r.status_code,200)
            self.assertIn(b'https://meet.jit.si/disciple-live-',r.data)
            self.assertIn(b'John 15',r.data)
            for page in ('/profile','/resources','/dashboard'):
                self.assertEqual(client.get(page).status_code,200)
        self.assertIn(b'Abide',self.member.get('/resources?q=Abide').data)
        self.assertNotIn(b'Abide',self.member.get('/resources?q=unmatched').data)
        self.post(self.member,f'/sessions/{meeting}/cancel')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM sessions'),0)
        self.assertEqual(self.scalar('SELECT is_booked FROM availability_slots'),0)
        self.post(self.member,'/book-session',slot_id=slot)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM sessions'),1)

    def test_permissions_and_csrf(self):
        self.connect(); slot=self.slot()
        self.post(self.member,'/book-session',slot_id=slot)
        meeting=self.scalar('SELECT id FROM sessions')
        r=self.other.get(f'/session/{meeting}',follow_redirects=True)
        self.assertNotIn(b'https://meet.jit.si/disciple-live-',r.data)
        self.assertEqual(self.post(self.other,f'/sessions/{meeting}/cancel').status_code,404)
        self.post(self.other,'/upload-content',title='Unauthorized',external_url='https://example.org')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM content_modules'),0)
        self.assertEqual(self.member.post('/logout').status_code,400)
        self.assertEqual(self.member.get('/logout').status_code,405)
        self.assertEqual(self.member.get('/dashboard').status_code,200)

    def test_booking_constraints(self):
        slot=self.slot()
        self.post(self.member,'/book-session',slot_id=slot)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM sessions'),0)
        self.connect()
        self.assertEqual(self.post(self.member,'/book-session',slot_id=slot,content_module_id=999).status_code,400)
        self.post(self.member,'/book-session',slot_id=slot)
        self.post(self.member,'/book-session',slot_id=slot)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM sessions'),1)
        self.assertEqual(self.post(self.mentor,f'/slots/{slot}/delete').status_code,404)
        self.assertEqual(self.post(self.member,'/book-session',slot_id='bad').status_code,400)
        self.post(self.mentor,'/add-slot',slot_time='2020-01-01T12:00')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM availability_slots'),1)

    def test_upload_validation_and_private_download(self):
        r=self.post(self.admin,'/upload-content',title='Unsafe',external_url='javascript:alert(1)')
        self.assertEqual(r.status_code,400)
        self.post(self.admin,'/upload-content',title='Study',content_file=(io.BytesIO(b'Study notes'), 'study.txt'))
        filename=self.scalar('SELECT file_path FROM content_modules')
        self.assertEqual(app.test_client().get('/uploads/'+filename).status_code,302)
        r=self.member.get('/uploads/'+filename)
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.data,b'Study notes')
        self.assertIn('attachment',r.headers['Content-Disposition'])
        r.close()
        module=self.scalar('SELECT id FROM content_modules')
        self.post(self.admin,f'/resources/{module}/delete')
        self.assertEqual(self.member.get('/uploads/'+filename).status_code,404)

    def test_profile_and_password_revocation(self):
        second=app.test_client()
        self.post(second,'/login',email='member@example.org',password=PASSWORD)
        r=self.post(self.member,'/profile',name='Daniel',bio='Grow in faith',timezone='America/Los_Angeles',current_password=PASSWORD,new_password='A different long password!')
        self.assertEqual(r.status_code,200)
        self.assertIn(b'Daniel',self.member.get('/dashboard').data)
        self.assertEqual(second.get('/dashboard').status_code,302)
        r=self.post(self.member,'/profile',name='Daniel',bio='',timezone='Invalid/Zone')
        self.assertEqual(r.status_code,400)

    def test_invalid_registration_and_rate_limit(self):
        client=app.test_client()
        self.post(client,'/register',name='',email='invalid',password='short',role='admin')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM users'),4)
        for _ in range(10):
            self.post(client,'/login',email='missing@example.org',password='wrong')
        self.assertEqual(self.post(client,'/login',email='missing@example.org',password='wrong').status_code,429)
        self.assertEqual(client.get('/health').json,{'status':'ok'})


    def test_concurrent_booking_has_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        self.connect()
        mentor = self.scalar("SELECT id FROM users WHERE role='disciplier'")
        self.post(self.other,'/request-mentor',discipler_id=mentor)
        req = self.scalar('SELECT MAX(id) FROM mentor_requests')
        self.post(self.mentor,f'/respond-request/{req}',decision='accepted')
        slot = self.slot()
        def book(client):
            return self.post(client,'/book-session',slot_id=slot).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(book, (self.member, self.other)))
        self.assertEqual(statuses,[200,200])
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM sessions'),1)

    def test_time_zones_and_duplicate_availability(self):
        self.post(self.mentor,'/profile',name='Mentor',bio='',timezone='America/Los_Angeles')
        self.post(self.mentor,'/add-slot',slot_time='2030-01-15T10:00')
        self.assertEqual(self.scalar('SELECT slot_time FROM availability_slots'),'2030-01-15T18:00:00+00:00')
        self.post(self.mentor,'/add-slot',slot_time='2030-01-15T10:30')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM availability_slots'),1)
        self.post(self.mentor,'/add-slot',slot_time='2030-03-10T02:30')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM availability_slots'),1)
        self.post(self.mentor,'/add-slot',slot_time='2030-11-03T01:30')
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM availability_slots'),1)

if __name__=='__main__':
    unittest.main()
