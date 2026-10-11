"""Small public build fixture; no engine, signing or native qualification."""
import hashlib,json

def canonical(value):return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode()
def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def file(path,body,mode=420):return {'path':path,'kind':'regular','bytes':len(body),'sha256':hashlib.sha256(body).hexdigest(),'mode':mode}

def build_input():
 data={'source/module.py':b'pass\n','recipe/build.py':b'pass\n','deps/lock.json':b'{}\n',
       'contracts/descriptor.json':canonical({'module_id':'api','module_version':'0.1.020','interface_version':1}),
       'contracts/compatibility.json':canonical({'version':1}),
       'contracts/schema.json':canonical({'version':1,'migrations':[]}),
       'contracts/api.json':canonical({'version':1}),
       'contracts/events.json':canonical({'version':1}),
       'contracts/event-v1.json':canonical({'version':1})}
 rows={path:file(path,body) for path,body in data.items()}
 value={'schema':'cortex.build-input.v1','format_version':1,
  'target':{'module_id':'api','module_version':'0.1.020','oci_platform':'linux/amd64'},
  'source':{'repository':'Kaidera-AI/cortex','revision':'a'*40,'tree':'b'*40,'context_root':'source','inventory':[rows['source/module.py']]},
  'descriptor':rows['contracts/descriptor.json'],
  'recipe':{'recipe_id':'api-oci','version':1,'entrypoint':'recipe/build.py','inventory':[rows['recipe/build.py']],'argv_template':['python3','recipe/build.py']},
  'dependencies':{'locks':[rows['deps/lock.json']],'assets':[],'base_images':[]},
  'builder_requirements':{'execution_kind':'native','os':'linux','architecture':'amd64','rootless':True,'uid_min':1,
    'toolchain':{'compiler_id':'fixture-python','version':'3.12.0','sha256':'c'*64},
    'engine':{'engine_id':'fixture-engine','version':'1.0','sha256':'d'*64},
    'storage_policy':{'fresh_private':True,'mutable_shared':False}},
  'determinism':{'policy_id':'fixture-policy','version':1,'epoch':0,'clock_channel':'engine_timestamp',
    'python':{'applicable':True,'cache_mode':'CHECKED_HASH','final_root':'/app','optimization':0,'hash_seed':0,'compiler_sha256':'c'*64},
    'file_metadata':{'uid':1001,'gid':1001,'mtime_epoch':0,'xattrs':'deny','acls':'deny'},
    'account_log_cache':{'policy':'deny-generated'},'serialization':{'json':'canonical-v1','tar':'uncompressed','compression':'none'},
    'environment':{'names':['PYTHONHASHSEED']}},
  'resources':{'cpu_millicores':2000,'memory_bytes':1073741824,'disk_bytes':1073741824,
    'phase_timeouts_sec':{'input':30,'parser':30,'build':120,'export':30,'audit':30,'cleanup':30},'absolute_timeout_sec':300},
  'compatibility_inputs':{'compatibility_contract':rows['contracts/compatibility.json'],'schema_manifest':rows['contracts/schema.json'],
    'api_descriptor':rows['contracts/api.json'],'event_index':rows['contracts/events.json'],'event_schemas':[rows['contracts/event-v1.json']],'dependency_interfaces':[]},
  'admission_policy':{'policy_id':'fixture-policy','version':1,'policy_sha256':'e'*64,'qualification_scope':'fixture'}}
 return value,data
