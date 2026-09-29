-- Rule statistics from `hledger-review rules`, shown as virtual text at the
-- end of each rule in an hledger CSV rules file. Updates while you type.

local M = {}

M.config = {
  cmd = { "hledger-review", "rules" },
  pattern = { "*.csv.rules" },
  debounce = 300, -- ms after the last change
  highlights = { note = "Comment", warning = "DiagnosticWarn", error = "DiagnosticError" },
}

local ns = vim.api.nvim_create_namespace("hledger-review")
local enabled = true
local timers = {} -- buffer -> uv timer
local runs = {} -- buffer -> number of the latest run, so stale output is dropped

-- display
local function show(buf, out)
  vim.api.nvim_buf_clear_namespace(buf, ns, 0, -1)
  local count = vim.api.nvim_buf_line_count(buf)
  for line in vim.gsplit(out, "\n", { trimempty = true }) do
    local lnum, level, text = line:match(":(%d+): (%a+): (.*)$")
    lnum = tonumber(lnum)
    if lnum and lnum <= count then
      vim.api.nvim_buf_set_extmark(buf, ns, lnum - 1, 0, {
        virt_text = { { "  " .. text, M.config.highlights[level] or "Comment" } },
        virt_text_pos = "eol",
      })
    end
  end
end

-- running the command
--- Recompute the stats for BUF from its current, possibly unsaved, text.
function M.refresh(buf, verbose)
  buf = (buf == nil or buf == 0) and vim.api.nvim_get_current_buf() or buf
  local file = vim.api.nvim_buf_get_name(buf)
  if not enabled or file == "" then
    return
  end
  runs[buf] = (runs[buf] or 0) + 1
  local run = runs[buf]
  local text = table.concat(vim.api.nvim_buf_get_lines(buf, 0, -1, false), "\n") .. "\n"
  local cmd = vim.list_extend(vim.deepcopy(M.config.cmd), { "--rules", file, "--stdin" })
  local ok, err = pcall(vim.system, cmd, { stdin = text, text = true, cwd = vim.fs.dirname(file) }, function(res)
    vim.schedule(function()
      if run ~= runs[buf] or not vim.api.nvim_buf_is_valid(buf) then
        return
      end
      if res.code ~= 0 then
        vim.api.nvim_buf_clear_namespace(buf, ns, 0, -1)
        if verbose then
          vim.notify(vim.trim(res.stderr), vim.log.levels.WARN, { title = "hledger-review" })
        end
        return
      end
      show(buf, res.stdout)
      if verbose then
        vim.notify(vim.trim(res.stderr), vim.log.levels.INFO, { title = "hledger-review" })
      end
    end)
  end)
  if not ok and verbose then
    vim.notify(tostring(err), vim.log.levels.WARN, { title = "hledger-review" })
  end
end

local function debounced(buf)
  local timer = timers[buf]
  if not timer then
    timer = assert(vim.uv.new_timer())
    timers[buf] = timer
  end
  timer:stop()
  timer:start(M.config.debounce, 0, vim.schedule_wrap(function()
    if vim.api.nvim_buf_is_valid(buf) then
      M.refresh(buf)
    end
  end))
end

--- Show or hide the stats in every rules buffer.
function M.toggle()
  enabled = not enabled
  for _, buf in ipairs(vim.api.nvim_list_bufs()) do
    vim.api.nvim_buf_clear_namespace(buf, ns, 0, -1)
  end
  if enabled then
    M.refresh(0, true)
  end
end

function M.setup(opts)
  M.config = vim.tbl_extend("force", M.config, opts or {})
  local group = vim.api.nvim_create_augroup("hledger-review", { clear = true })
  vim.api.nvim_create_autocmd({ "BufReadPost", "BufWritePost" }, {
    group = group,
    pattern = M.config.pattern,
    callback = function(ev)
      M.refresh(ev.buf)
    end,
  })
  vim.api.nvim_create_autocmd({ "TextChanged", "TextChangedI" }, {
    group = group,
    pattern = M.config.pattern,
    callback = function(ev)
      debounced(ev.buf)
    end,
  })
  vim.api.nvim_create_autocmd("BufWipeout", {
    group = group,
    pattern = M.config.pattern,
    callback = function(ev)
      if timers[ev.buf] then
        timers[ev.buf]:close()
        timers[ev.buf] = nil
      end
      runs[ev.buf] = nil
    end,
  })
  vim.api.nvim_create_user_command("HledgerReviewRules", function(cmd)
    if cmd.args == "toggle" then
      M.toggle()
    else
      M.refresh(0, true)
    end
  end, {
    nargs = "?",
    complete = function()
      return { "toggle" }
    end,
    desc = "Refresh (or toggle) hledger-review rule stats",
  })
end

return M
