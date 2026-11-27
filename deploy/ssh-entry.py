#!/usr/bin/python3
"""Install root-owned at /usr/local/bin/humanity-deploy-entry."""
import os,re,sys
command=os.environ.get('SSH_ORIGINAL_COMMAND','')
match=re.fullmatch(r'deploy ([0-9a-f]{40})',command)
if not match:
    print('Only deploy <40-character commit SHA> is supported.',file=sys.stderr);sys.exit(1)
os.execvp('sudo',['sudo','-n','/usr/local/sbin/humanity-deploy',match[1]])
