#!/bin/bash
# Oracle: apply the reference implementation to the untouched base.
set -euo pipefail
cd /app
git apply --whitespace=nowarn /solution/reference.diff
