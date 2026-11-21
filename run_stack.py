"""Forward container signals and make fatal database initialization fail the job."""

import signal
import subprocess
import sys
import time

from galaxy_runtime import DATABASE_FAILED


def main():
    supervisor = subprocess.Popen(
        ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf", "-n"]
    )

    def stop(signum, frame):
        if supervisor.poll() is None:
            supervisor.send_signal(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    failed = False
    while supervisor.poll() is None:
        if DATABASE_FAILED.exists() and not failed:
            failed = True
            supervisor.terminate()
        time.sleep(0.25)
    return 1 if failed else supervisor.returncode


if __name__ == "__main__":
    sys.exit(main())
