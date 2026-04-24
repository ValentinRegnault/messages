web: bin/scalingo_run_web
worker: python worker.py --concurrency=4 --exclude=imports,reindex
workerhuge: python worker.py --concurrency=2 --queues=imports,reindex --disable-scheduler
postdeploy: python manage.py migrate
