#!/usr/bin/bash

# $APP defines where's the module (or package)
if [[ -z ${APP} ]]; then
    echo "APP not defined or empty, exiting"
    exit 1
fi

# https://www.shellcheck.net/wiki/SC1091
# shellcheck source=/dev/null
source /usr/bin/setup_env_in_openshift.sh

mkdir -p "${PACKIT_HOME}/.ssh"
chmod 0700 "${PACKIT_HOME}/.ssh"
pushd "${PACKIT_HOME}/.ssh" || exit
install -m 0400 /packit-ssh/id_rsa .
install -m 0400 /packit-ssh/id_rsa.pub .
if [[ -f /packit-ssh/config ]]; then install -m 0400 /packit-ssh/config .; fi
grep -q pkgs.fedoraproject.org known_hosts || ssh-keyscan pkgs.fedoraproject.org >>known_hosts
popd || exit

DEFAULT_CELERY_COMMAND="worker"
# Whether to run Celery worker or beat (task scheduler)
CELERY_COMMAND="${CELERY_COMMAND:-$DEFAULT_CELERY_COMMAND}"

if [[ "${CELERY_COMMAND}" == "beat" ]]; then
    # when using the database backend, celery beat must be running for the results to be expired.
    # https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html#starting-the-scheduler
    exec celery --app="${APP}" beat --loglevel="${LOGLEVEL:-DEBUG}" --pidfile=/tmp/celerybeat.pid --schedule=/tmp/celerybeat-schedule

elif [[ "${CELERY_COMMAND}" == "worker" ]]; then
    # define queues to serve
    DEFAULT_QUEUES="short-running,long-running"
    QUEUES="${QUEUES:-$DEFAULT_QUEUES}"

    # Number of concurrent worker threads executing tasks.
    DEFAULT_CONCURRENCY="1"
    CONCURRENCY="${CONCURRENCY:-$DEFAULT_CONCURRENCY}"

    # Options: prefork | eventlet | gevent | solo
    # https://www.distributedpython.com/2018/10/26/celery-execution-pool/
    DEFAULT_POOL="solo"
    POOL="${POOL:-$DEFAULT_POOL}"

    # if this worker serves the long-running queue, it needs the repository cache
    if [[ "$QUEUES" == *"long-running"* ]]; then
      # Can't be set during deployment
      SANDCASTLE_REPOSITORY_CACHE_VOLUME="sandcastle-repository-cache-$(uname --nodename)"
      export SANDCASTLE_REPOSITORY_CACHE_VOLUME
    fi

    # https://docs.celeryq.dev/en/stable/userguide/optimizing.html#optimizing-prefetch-limit
    exec celery --app="${APP}" worker --loglevel="${LOGLEVEL:-DEBUG}" --concurrency="${CONCURRENCY}" --pool="${POOL}" --prefetch-multiplier=1 --queues="${QUEUES}"
fi
