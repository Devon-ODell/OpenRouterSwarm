" strudel.nvim — filetype detection for Strudel livecoding language.
" Strudel is a JS dialect (strudel.cc). We only claim files that explicitly
" ask for it: *.strudel and *.ss — never plain .js.

au! BufNewFile,BufRead *.strudel setlocal filetype=strudel
au! BufNewFile,BufRead *.ss      setlocal filetype=strudel