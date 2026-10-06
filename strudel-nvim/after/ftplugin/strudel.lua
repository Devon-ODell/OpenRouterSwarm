--- strudel.nvim — filetype-level options for .strudel buffers.
--- Loaded after any other ftplugin; keeps our indent/options authoritative.
vim.opt_local.expandtab = true
vim.opt_local.shiftwidth = 2
vim.opt_local.tabstop = 2
vim.opt_local.commentstring = "// %s"

-- Syntax reload so the pastel groups paint as soon as the file opens.
vim.cmd("syntax enable")

-- Friendly filetype title shown by our winbar/statusline.
vim.b.strudel = true