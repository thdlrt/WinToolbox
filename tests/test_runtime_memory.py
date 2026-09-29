"""The real RPC entrypoint configures BLAS before optional audio is imported."""
import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize('override, expected', [(None, '1'), ('3', '3')])
def test_backend_blas_default_and_explicit_override(override, expected):
    env = dict(os.environ)
    env.pop('OPENBLAS_NUM_THREADS', None)
    if override:
        env['OPENBLAS_NUM_THREADS'] = override
    code = '''
import os, sys, json
import toolbox.__main__
print(json.dumps([os.environ['OPENBLAS_NUM_THREADS'], 'numpy' in sys.modules]))
'''
    result = subprocess.run([sys.executable, '-c', code], env=env, text=True,
                            capture_output=True, check=True, timeout=15)
    assert json.loads(result.stdout) == [expected, False]
