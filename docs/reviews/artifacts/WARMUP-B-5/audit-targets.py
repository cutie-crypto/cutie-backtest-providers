import sys,json,tempfile,subprocess,hashlib
from pathlib import Path
sys.path[:0]=['backtesting-py','backtesting-py/tests','backtesting-py/tests/fixtures']
import pytest
from unittest.mock import patch,DEFAULT
from starlette.testclient import TestClient
import cutie_backtesting_provider as p
from _complete_seed_fixture import extend_seed
mode=sys.argv[1];p.WARMUP_SNAPSHOT_DIR=Path(tempfile.mkdtemp())
if mode=='old':
 exec(compile(subprocess.check_output(['git','show','678b4e1:backtesting-py/cutie_backtesting_provider.py'],text=True),p.__file__,'exec'),p.__dict__)
 p.WARMUP_SNAPSHOT_DIR=Path(tempfile.mkdtemp())
current=[''];rows=[];main=[None];orig_set=pytest.MonkeyPatch.setattr;orig_obj=patch.object

def adapt(attr,value):
 if attr=='_fetch_ohlcv':
  def fetch(*a,**k):
   result=value(*a,**k);main[0]=result.copy();return result
  return fetch
 if attr=='_fetch_template_warmup' and mode=='new':
  return lambda *a,**k:extend_seed(value(*a),a[3],a[5],a[6])
 return value

def set_(self,target,name,value,*a,**k):
 if target is p:value=adapt(name,value)
 return orig_set(self,target,name,value,*a,**k)
def obj_(target,attr,new=DEFAULT,*a,**k):
 if target is p and new is not DEFAULT:new=adapt(attr,new)
 return orig_obj(target,attr,new,*a,**k)
pytest.MonkeyPatch.setattr=set_;patch.object=obj_
original=TestClient.post

def post(self,url,*a,**k):
 out=original(self,url,*a,**k)
 if str(url).endswith('/cutie/backtest'):
  body=out.json();rows.append({'nodeid':current[0],'request':k.get('json'),'body':body,'main_sha256':hashlib.sha256(main[0].to_json(orient='split',date_unit='ns',double_precision=15).encode()).hexdigest() if main[0] is not None else None})
 return out
TestClient.post=post
class Plugin:
 def pytest_runtest_setup(self,item):current[0]=item.nodeid
files=['backtesting-py/tests/test_patconf2a_wiring.py','backtesting-py/tests/test_time_layer_6e2.py','backtesting-py/tests/test_chan_slim_evidence.py']
code=pytest.main([*files,'-q','-p','no:cacheprovider'],plugins=[Plugin()])
Path('/tmp/warmup-b5/targets-'+mode+'.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
print('CAPTURED',len(rows),'PYTEST_EXIT',code)
