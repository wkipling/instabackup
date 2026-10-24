#!/bin/sh
set -e
export JAVA_HOME=/home/wkipling/insta360backuptool/tools/jdk
export PATH="$JAVA_HOME/bin:$PATH"
exec /home/wkipling/insta360backuptool/tools/ghidra/ghidra_12.2_PUBLIC/support/analyzeHeadless \
  /home/wkipling/insta360backuptool/analysis/ghidra-aip AIP \
  -process instaAIP -noanalysis \
  -scriptPath /home/wkipling/insta360backuptool/tools/ghidra-scripts \
  -postScript "$@"
