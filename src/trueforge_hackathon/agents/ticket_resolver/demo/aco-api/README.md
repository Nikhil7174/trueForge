# aco-api

Care-team lookups for a Medicare ACO: find a beneficiary by MBI, list their claims, total what was paid, and list a
primary-care NPI's attributed roster. Python standard library only; data lives in SQLite.

This is the demo codebase the **ticket-resolver** agent reproduces bugs against. The data is synthetic (5,000
beneficiaries, 15,000 claims, generated with a fixed seed). No real PHI.

```bash
python -m aco_api build                        # (re)create .data/aco.db
python -m aco_api patient 1EG4TE5MK72
python -m aco_api claims-total 1EG4TE5MK72 --year 2025
python -m aco_api attributed-count
python -m unittest discover -s tests           # test suite
```

Set `ACO_DB` to use a different database file.
