import copy
import tempfile
import unittest
from cryptography.fernet import InvalidToken
from flowhub.db import Database
from flowhub.connection_bundle import encrypt,decrypt,import_stores

class BundleTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.db=Database(self.tmp.name)
  self.payload={'stores':[{'name':'Test','kind':'maozi','config':{'shop_id':'1','warehouse_id':'2'},'credentials':{'client_id':'123','api_key':'private-example','erp_token':'erp-example'},'verified':True}]}
 def test_crypto(self):
  text=encrypt(self.payload,'correct-unlock-code')
  self.assertNotIn('private-example',text)
  self.assertEqual(decrypt(text,'correct-unlock-code'),self.payload)
  with self.assertRaises(InvalidToken):decrypt(text,'incorrect-unlock-code')
  import json
  x=json.loads(text);x['data']=x['data'][:-8]+'AAAAAAAA'
  with self.assertRaises(InvalidToken):decrypt(json.dumps(x),'correct-unlock-code')
 def test_import_idempotent_disabled(self):
  import_stores(self.db,self.payload);import_stores(self.db,self.payload)
  with self.db.connect() as c:
   self.assertEqual(c.execute('select count(*) from stores').fetchone()[0],1)
   r=c.execute('select * from stores').fetchone()
   self.assertEqual(r['enabled'],0);self.assertEqual(r['verified'],1)
   self.assertNotIn('private-example',r['secret'])
   self.assertEqual(c.execute('select enabled from workflows').fetchone()[0],0)
 def test_running_rejected(self):
  with self.db.connect() as c:c.execute('update workflows set enabled=1')
  with self.assertRaises(ValueError):import_stores(self.db,self.payload)
  with self.db.connect() as c:self.assertEqual(c.execute('select count(*) from stores').fetchone()[0],0)
 def test_bad_batch_atomic(self):
  p=copy.deepcopy(self.payload);p['stores'].append(copy.deepcopy(p['stores'][0]))
  with self.assertRaises(ValueError):import_stores(self.db,p)
  with self.db.connect() as c:self.assertEqual(c.execute('select count(*) from stores').fetchone()[0],0)
 def test_non_admin_rejected(self):
  with self.db.connect() as c:self.db.create_user(c,'other','test-password')
  with self.assertRaises(ValueError):import_stores(self.db,self.payload,'other')
 def test_owner_isolation(self):
  with self.db.connect() as c:self.db.create_user(c,'second','test-password','admin')
  import_stores(self.db,self.payload)
  import_stores(self.db,self.payload,'second')
  with self.db.connect() as c:self.assertEqual(c.execute('select count(distinct owner) from stores').fetchone()[0],2)
if __name__=='__main__':unittest.main()
