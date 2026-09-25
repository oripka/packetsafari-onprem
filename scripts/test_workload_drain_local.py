#!/usr/bin/env python3
"""Real Docker warm-drain regression with synthetic jobs; no AI or app data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packetsafari_onprem import workload_drain as drain
from packetsafari_onprem.rolling_update import save


def main():
    root = Path(os.environ.get('PACKETSAFARI_DATA_ROOT', '/Users/otr/packetsafari-data')) / 'runs/deployment-proxy' / ('worker-drain-' + uuid.uuid4().hex)
    root.mkdir(parents=True)
    supervisor = Path(__file__).resolve().parents[2] / 'packetsafari/backend/scripts/worker_supervisor.sh'
    job = root / 'job.py'
    job.write_text('''import os,signal,sys,time
from pathlib import Path
name,delay,code=sys.argv[1:]
def term(*_):
 with Path('/evidence/'+name+'.signals').open('a') as stream: stream.write('TERM\\n')
signal.signal(signal.SIGTERM, term)
Path('/evidence/'+name+'.started').write_text(str(os.getpid()))
time.sleep(float(delay))
Path('/evidence/'+name+'.finished').write_text(str(time.time()))
raise SystemExit(int(code))
''')
    image = subprocess.check_output(['docker', 'image', 'inspect', 'python:3.14-slim-bookworm', '--format', '{{.Id}}'], text=True).strip()
    results = []
    for failure in (False, True):
        label = 'failed' if failure else 'success'
        evidence = root / label
        evidence.mkdir()
        container = 'ps-drain-' + root.name[-10:] + '-' + label
        command = f'''set -eu
source /supervisor.sh
python /job.py fast 2 {3 if failure else 0} & PIDS+=($!)
python /job.py slow 7 0 & PIDS+=($!)
worker_wait
'''
        subprocess.run(['docker', 'run', '-d', '--name', container, '--restart', 'always',
                        '-v', f'{supervisor}:/supervisor.sh:ro', '-v', f'{job}:/job.py:ro',
                        '-v', f'{evidence}:/evidence', image, 'bash', '-c', command], check=True, capture_output=True)
        for _ in range(100):
            if (evidence/'fast.started').exists() and (evidence/'slow.started').exists():
                break
            time.sleep(.1)
        else:
            raise RuntimeError('Fixture jobs did not start')
        receipt = drain.begin_worker(container)
        receipt = drain.request_worker(receipt, lambda value: save(evidence/'receipt.json', value))
        assert drain.wait_worker(receipt, 0)['status'] == 'draining'
        time.sleep(3)
        assert drain.proxy.inspect(container)['State']['Running'], 'Supervisor abandoned slow job'
        # Repeated update after interrupted signaling must not escalate shutdown.
        receipt = drain.request_worker(receipt, lambda value: save(evidence/'receipt.json', value))
        try:
            result = drain.wait_worker(receipt, 12)
            assert not failure and result['status'] == 'drained'
        except RuntimeError:
            assert failure
            result = {'status': 'failed-as-expected'}
        assert (evidence/'slow.finished').exists(), 'Slow job was killed'
        assert (evidence/'slow.signals').read_text().splitlines() == ['TERM'], 'Drain must signal each child exactly once'
        info = drain.proxy.inspect(container)
        assert not info['State']['Running']
        assert info['HostConfig']['RestartPolicy']['Name'] == 'no'
        results.append({'case': label, **result, 'exitCode': info['State']['ExitCode']})
    save(root/'result.json', {'checks': results, 'evidence': str(root)})
    print(json.dumps({'checks': results, 'evidence': str(root)}, indent=2))


if __name__ == '__main__':
    main()
