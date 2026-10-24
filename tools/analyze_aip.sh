#!/bin/sh
set -e
export JAVA_HOME=/home/wkipling/insta360backuptool/tools/jdk
export PATH="$JAVA_HOME/bin:$PATH"
exec /home/wkipling/insta360backuptool/tools/ghidra/ghidra_12.2_PUBLIC/support/analyzeHeadless \
  /home/wkipling/insta360backuptool/analysis/ghidra-aip AIP \
  -import /home/wkipling/insta360backuptool/vendor/firmware/extract/843464317/linux/usr/bin/instaAIP \
  -overwrite -analysisTimeoutPerFile 2000
