# /etc/cron.d/__SREGYM_SERVICE__
SHELL=/bin/sh
PATH=/usr/local/bin:/usr/bin:/bin
# m   h  dom mon dow  user  command
*/15  *  *   *   *    app   cd __SREGYM_REPO__ && __SREGYM_PYTHON__ scripts/expire_carts.py >> logs/cron.log 2>&1
30    3  *   *   *    app   cd __SREGYM_REPO__ && sqlite3 __SREGYM_CORE_DB__ 'PRAGMA optimize;' >> logs/cron.log 2>&1
0     4  *   *   0    app   find __SREGYM_REPO__/logs -name '*.log.*' -mtime +14 -delete
15    2  *   *   1    app   cd __SREGYM_REPO__ && sqlite3 data/ledger.db ".backup data/ledger-snapshot-$(date +\%Y\%m\%d).db" >> logs/cron.log 2>&1
