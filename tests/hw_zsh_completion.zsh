#!/bin/zsh -f
# Manual check (needs the device): drive an interactive zsh through a pty, press
# Tab after partial command lines, and print what the command line became.
#   zsh tests/hw_zsh_completion.zsh
# The expected lines use documents on the author's device; adjust them to yours.
emulate -L zsh
zmodload zsh/zpty
repo=${0:A:h:h}

zpty -b z "env LANG=${LANG:-en_US.UTF-8} PATH=$repo/.venv/bin:$HOME/bin:\$PATH zsh -f -i"
zpty -w z "PROMPT='>'; fpath=($repo/completions \$fpath); autoload -Uz compinit; compinit -u -D"
zpty -w z 'zstyle ":completion:*" list-prompt ""; zstyle ":completion:*" menu no'
zpty -w z 'show-buf() { print -r -- "<<$BUFFER>>"; }; zle -N show-buf; bindkey "^X" show-buf'
zpty -w z 'print READY'
ready=""; for i in {1..50}; do zpty -rt z chunk 2>/dev/null && ready+=$chunk; [[ $ready == *$'\nREADY'* ]] && break; sleep 0.2; done
sleep 0.5

drain() { local chunk; while zpty -rt z chunk 2>/dev/null; do out+=$chunk; done }

try() {  # $1 = what to type before Tab, $2 = expected command line after Tab
  local out=""
  drain; out=""
  zpty -w -n z "$1"$'\t'
  sleep ${3:-2}
  zpty -w -n z $'\C-x'
  sleep 0.5
  drain
  local got=${${(M)out##*<<}#<<}; got=${${got%%>>*}}
  got=${${out##*<<}%%>>*}
  if [[ $got == "$2" ]]; then print -r -- "ok    [$1⇥] -> [$got]"
  else print -r -- "FAIL  [$1⇥] -> [$got]   expected [$2]"; fail=1; fi
  zpty -w -n z $'\C-u'
}

fail=0
try 'quaderno l'                          'quaderno ls '
try 'quaderno sc'                         'quaderno screenshot '
try 'quaderno ls Bo'                      'quaderno ls Books/'
try 'quaderno ls Books/milit'             'quaderno ls Books/Military\ History/'
try "quaderno ls 'Books/Milit"            "quaderno ls 'Books/Military History/"
try 'quaderno put x.pdf -d Books/Milit'   'quaderno put x.pdf -d Books/Military\ History/'
try 'quaderno get Books/Military\ History/Jag' \
    'quaderno get Books/Military\ History/Jagdpanther\ vs\ SU-100\ Eastern\ Front,\ 1945\ \(David\ R.\ Higgins,\ Richard\ Chasemore\)\ \(z-lib.org\).pdf '
try 'quaderno mv Books/Fight'             'quaderno mv Books/Fighter\ Combat\ -\ Tactics\ and\ Maneuvering\ --\ Robert\ L_\ Shaw\ --\ 1988\ --\ Naval\ Institute\ Press\ --\ 9780870210594\ --\ cdf446d4aa8b1e83b4cd4dc824de74f8\ --\ Anna’s\ Archive.pdf '
try 'quaderno sync /tmp/x Sam'            'quaderno sync /tmp/x Samples/'
try "quaderno ls 'Fight*"                 "quaderno ls 'Fight*"
# --config before the command is passed on: the paired folder completes, a folder
# without credentials completes nothing
try 'quaderno --config ~/.config/quaderno ls Bo'   'quaderno --config ~/.config/quaderno ls Books/'
try 'quaderno --config /nonexistent ls Bo'          'quaderno --config /nonexistent ls Bo'
zpty -d z
exit $fail
