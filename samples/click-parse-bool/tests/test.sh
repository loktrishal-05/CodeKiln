#!/bin/bash
# Grades the repo after the agent (or oracle) ran. Reward is written by reward.py.
set -u
mkdir -p /logs/verifier
cd /app
# Anti-gaming: grade against the pristine base test suite, never one the agent edited.
# ponytail: same-container restore; Harbor's separate verifier mode is the stronger upgrade.
git checkout --quiet "$(git rev-list --max-parents=0 HEAD | tail -n 1)" -- tests
git clean --quiet -fdx -- tests
cp -r /tests/feature/. tests/
python -m pytest -p no:cacheprovider -rA --tb=short --continue-on-collection-errors tests > /logs/verifier/pytest.txt 2>&1
python /tests/reward.py /logs/verifier/pytest.txt /tests/fail_to_pass.json /tests/pass_to_pass.json /logs/verifier
