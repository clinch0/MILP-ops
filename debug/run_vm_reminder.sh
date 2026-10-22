set -eu
cd "$(dirname "$0")/.."
mkdir -p debug/runs/sip-outbound-reminder
python3 tools/check_space.py --write-path . --expected-gib 1
docker compose -f compose.yaml -f compose.vm.yaml --profile telephony build ari-bridge > debug/runs/sip-outbound-reminder/build.log 2>&1
docker compose -f compose.yaml -f compose.vm.yaml --profile telephony up -d --no-build --no-deps ari-bridge
docker compose exec -T crm python - <<'PY'
import sqlite3,os,json
from pathlib import Path
c=sqlite3.connect(os.environ['CRM_DB'])
r=c.execute("SELECT id,phone,last_name,first_name,birth_date FROM patients WHERE card_no='1002'").fetchone()
assert r and r[2]=='Иванов' and r[3]=='Иван' and r[4]=='1990-01-01','Synthetic fixture mismatch: no contact changed'
Path('/data/reminder-test-contact.json').write_text(json.dumps({'id':r[0],'phone':r[1]}))
c.execute('UPDATE patients SET phone=? WHERE id=?',('phone-114',r[0]));c.commit();c.close()
print('Assigned own test softphone to verified synthetic fixture only.')
PY
restore_contact() {
docker compose exec -T crm python - <<'PY'
import sqlite3,os,json
from pathlib import Path
r=json.loads(Path('/data/reminder-test-contact.json').read_text())
c=sqlite3.connect(os.environ['CRM_DB']);c.execute("UPDATE patients SET phone=? WHERE id=? AND phone='phone-114'",(r['phone'],r['id']));c.commit();c.close()
print('Synthetic contact restored.')
PY
}
trap restore_contact EXIT
crm_address=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' clinic-voice-crm-1)
docker run --rm -u 0 --network host --env-file .env -e CRM_URL="http://$crm_address:8080" -v "$PWD:/workspace" -v /opt/hackathon/sip:/organizer:ro -v /opt/hackathon/sip.env:/organizer-env/sip.env:ro -w /workspace --entrypoint python clinic-voice-crm debug/run_vm_reminder.py "$@" > debug/runs/sip-outbound-reminder/run.log 2>&1
tail -n 5 debug/runs/sip-outbound-reminder/run.log
python3 tools/check_space.py
