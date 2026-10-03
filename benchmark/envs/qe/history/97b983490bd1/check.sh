# pw.x and ld1.x from this environment; a real (tiny) run: the all-electron Si atom with ld1.x.
set -e
[ "$(command -v pw.x)" = "$FLOWER_ENV_PREFIX/bin/pw.x" ]
d=$(mktemp -d); trap 'rm -rf "$d"' EXIT; cd "$d"
printf "&input\n title='Si', zed=14., rel=2, config='[Ne] 3s2 3p2', iswitch=1, dft='PBE'\n/\n" > ld1.in
ld1.x < ld1.in > ld1.out 2>&1
grep -q "End of All-electron run" ld1.out && echo "ld1.x Si atom: OK"
grep -m1 "Program LD1" ld1.out || true
