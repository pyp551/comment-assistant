import sqlite3,unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from server import validate_resume_quarantine
class Quarantine(unittest.TestCase):
 def setUp(self):
  self.db=sqlite3.connect(':memory:')
  self.db.executescript('CREATE TABLE items(video,cid,status,updated); CREATE TABLE sends(video,cid,part,status,updated);')
 def tearDown(self):self.db.close()
 def seed(self):
  self.db.execute("INSERT INTO items VALUES('v','c','uncertain',123)")
  self.db.execute("INSERT INTO sends VALUES('v','c',1,'uncertain',123)")
  return {'owner_authorized_quarantine':True,'quarantine':{'items':[['v','c','uncertain',123]],'sends':[['v','c',1,'uncertain',123]]}}
 def test_empty_normal_resume(self):validate_resume_quarantine(self.db,{})
 def test_unknown_without_explicit_checkpoint_rejected(self):
  self.seed()
  with self.assertRaises(ValueError):validate_resume_quarantine(self.db,{})
 def test_exact_quarantine_preserves_records(self):
  checkpoint=self.seed();validate_resume_quarantine(self.db,checkpoint)
  self.assertEqual(self.db.execute('SELECT status FROM sends').fetchone()[0],'uncertain')
 def test_new_unknown_cannot_hide_in_old_checkpoint(self):
  checkpoint=self.seed();self.db.execute("INSERT INTO sends VALUES('v','new',1,'uncertain',124)")
  with self.assertRaises(ValueError):validate_resume_quarantine(self.db,checkpoint)
 def test_drafting_always_rejected(self):
  checkpoint=self.seed();self.db.execute("INSERT INTO items VALUES('v','d','drafting',125)")
  with self.assertRaises(ValueError):validate_resume_quarantine(self.db,checkpoint)
