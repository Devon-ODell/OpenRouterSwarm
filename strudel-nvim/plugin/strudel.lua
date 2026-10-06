-- strudel.nvim — entry point.
-- Kept as a tiny one-shot: all real work lives in lua/strudel/*.lu* so the
-- plugin is reloadable with :StrudelReload and testable headless.

vim.g.strudel_loaded = nil
require("strudel").setup()