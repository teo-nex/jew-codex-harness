# Routing improvements acceptance ledger

Each numbered feature is committed independently. Offline assertions are not
live provider or reasoning-enforcement proof. Existing services are untouched.

| Feature | Observable acceptance | Status |
| --- | --- | --- |
| 1 | Owned route edits, reorder, durable backup, crash recovery and rollback without a service restart; foreign edits refused | verified: `tests/test_core.py` hot-route tests |
| 2 | Catalog picks exact advertised IDs; missing models and cross-family aliases warned | pending |
| 3 | Latest request explained with sanitized routing, effort and timing evidence | pending |
| 4 | Project rules restrict transport/provider/payment and enforce final Astra | pending |
| 5 | Connect, first token, idle, total and attempt limits stop hung requests safely | pending |
| 6 | Bad payload does not poison account; auth/quota/transient cooldowns isolate accounts | pending |
| 7 | Opt-in fresh-client response/tool/effort checks and controlled recovery report evidence separately | pending |
