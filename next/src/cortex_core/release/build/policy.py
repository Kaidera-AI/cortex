"""Explicit behavioral RED baseline; no engine execution."""
def build_argv(engine,source,output,epoch):
 return [engine,'build','--timestamp',str(epoch),'--build-arg','SOURCE_DATE_EPOCH='+str(epoch),str(source)]
def verify_native(observed,platform):return True
def cleanup_paths(creator_uid,paths):return paths
