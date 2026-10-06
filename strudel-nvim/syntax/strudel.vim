" strudel.nvim — super lightweight syntax for .strudel files.
"
" Rather than depending on treesitter/JavaScript for a DSL that's mostly
" punctuation and numbers, we keep a regex-light mode that paints the parts
" that matter on a livecoding screen:
"
"   * numbers + hex (note/freq patterns like 0xdeadbeef or [0,3,7])  -> orange
"   * combinator chains ( .chain | >-> = )                           -> peach
"   * pulse/chord/root words                                        -> cyan/blue
"   * pattern-ish words (s, melody, scale, drone, ...)               -> purple
"   * comments (# ...)                                               -> dim

if exists("b:current_syntax")
  finish
endif

syn case ignore

" comments: # — but keep `#flip`-style tags visible via StrudelCommentTag
syn match strudelComment "\v#[^a-zA-Z0-9_].*$" contains=strudelCommentTag
syn match strudelCommentTag "\v#(flip|wid|cut|off|on|once|free|wait|note)\b" contained

" numbers: decimal + hex + floats (frequency/ratio values)
syn match strudelNumber "\v<0[xX][0-9a-fA-F_]+>"
syn match strudelNumber "\v<\d+\.?\d*([eE][+-]?\d+)?>"
syn match strudelNumber "\v<\.\d+>"

" strings (tempo names, scales, etc)
syn region strudelString start=+"+ end=+"+ skip=+\\\\"+
syn region strudelString start=+'+ end=+'+ skip=+\\\\'+

" combinator chain operators:  >->   =   <->   |->   ^  ...  (also .method)
syn match strudelCombinator "\v(>->|<->|\|->|-->|\^->|=>|==|\.)"
syn match strudelCombinator "\v[-+*/%&|<>=!]+"

" core vocabulary — paint as music-role words
syn keyword strudelPulse  pulse freq gain lpf hpf delay add clip
syn keyword strudelChord  chord melody scale viola cello piano bass drums hihat kick snare clap
syn keyword strudelPattern s arp tidalCycles superimpose transpose scaleChord
syn keyword strudelRoot   root

" JS control-ish words keep their music meaning
syn keyword strudelSymbol t 0xdeadbeef currentStep step

hi def link strudelNumber      Number
hi def link strudelString      String
hi def link strudelCombinator  StrudelCombinator
hi def link strudelPulse       StrudelPulse
hi def link strudelChord       StrudelChord
hi def link strudelPattern     StrudelPattern
hi def link strudelRoot        StrudelRoot
hi def link strudelComment     Comment
hi def link strudelCommentTag  StrudelCommentTag
hi def link strudelSymbol      StrudelSymbol

let b:current_syntax = "strudel"