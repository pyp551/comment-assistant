import unittest,tempfile,os,json,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import storage
class StorageTests(unittest.TestCase):
 def test_relocation_preserves_ledger_and_original(self):
  with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
   base=Path(temp).resolve();home=base/'home';home.mkdir();target=base/'target'
   (home/'comments.sqlite3').write_bytes(b'ledger');(home/'settings.json').write_text('{}');(home/'edge-profile').mkdir();(home/'edge-profile'/'cookie').write_text('private')
   with patch.object(storage,'HOME',home),patch.object(storage,'ROUTE',home/'data-location.json'),patch.dict(os.environ,{'COMMENT_WORKBENCH_PORT':'19876'},clear=True):
    storage.schedule(str(target));self.assertEqual(storage.current(),home);storage.relocate()
    self.assertEqual(storage.current(),target);self.assertEqual((target/'comments.sqlite3').read_bytes(),b'ledger');self.assertTrue((home/'comments.sqlite3').exists());self.assertFalse((target/'edge-profile').exists())
 def test_nonempty_target_rejected(self):
  with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
   base=Path(temp).resolve();home=base/'home';home.mkdir();target=base/'target';target.mkdir();(target/'existing').write_text('keep')
   with patch.object(storage,'HOME',home),patch.object(storage,'ROUTE',home/'data-location.json'),patch.dict(os.environ,{},clear=True):
    with self.assertRaises(ValueError):storage.schedule(str(target))
    with self.assertRaises(ValueError):storage.schedule(str(home/'child'))
    self.assertEqual((target/'existing').read_text(),'keep')
if __name__=='__main__':unittest.main()
