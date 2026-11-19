-- athena's actions in Rex's command palette. The line `athena palette install` puts in Rex's config loads this file; Rex reads
-- it again on `rex config reload`. This file only asks the questions: what is offered, what an answer may be and every write are
-- athena's (`athena palette ...`, `athena send`, `athena retire`). docs/palette.md has the whole story.
--
-- Each argument given skips its question, so `rex do` can run them with nobody there (all but showing a worker's window,
-- which only the Rex app that asked can do):
--   rex do athena_answer item=a12 option=yes        rex do athena_message worker=w text="..."
--   rex do athena_open target=pr:<url>              rex do athena_retire worker=w confirm=true
-- A question left unanswered (closed, timed out, no Rex app to ask it in) stops the action before it does anything, and says so.

local here = debug.getinfo(1, "S").source:gsub("^@", "")
local root = here:match("^(.*)/athena/caps/palette/palette%.lua$")
local ATHENA = root and (root .. "/bin/athena")
local ENV -- set by the line that loads this file: athena's state dir, whatever the Rex server's own environment says

local LIST_WAIT = 10 -- seconds for athena to say what to offer
local ACT_WAIT = 20 -- seconds to wait on a send or a retire before leaving it to finish on its own: Rex gives an action 30

local NO_ANSWER = {
  cancelled = "cancelled",
  timeout = "nobody answered in time",
  no_client = "no Rex app is connected to ask in",
  unsupported = "the Rex app can't show questions: update it",
  busy = "the Rex app is showing another question",
}

local function fail(title, text)
  error("athena: " .. title .. ": " .. text, 0)
end

-- A question with no answer: say why, and that nothing happened.
local function unanswered(title, why, detail, nothing)
  fail(title, (NO_ANSWER[why] or ("the question could not be shown: " .. tostring(detail or why))) .. "; " .. nothing .. ".")
end

local function athena_argv(title, args)
  if ATHENA == nil then
    fail(title, "can't find athena from " .. here)
  end
  if ENV == nil then
    fail(title, "load this file with the line `athena palette install` writes")
  end
  local argv = { ATHENA }
  for _, a in ipairs(args) do
    argv[#argv + 1] = a
  end
  return argv
end

-- What athena said: its error without the "athena: " it starts with, else what it printed.
local function said(r)
  return ((r.stderr ~= "" and r.stderr or r.stdout):gsub("^athena: ", ""):gsub("%s+$", ""))
end

-- An athena command that answers in JSON at once: a list, an answer queued, a page opened.
local function athena_json(title, args)
  local r, err = rex.exec(athena_argv(title, args), { timeout = LIST_WAIT, env = ENV })
  if r == nil then
    fail(title, "athena did not run: " .. tostring(err))
  end
  if not r.ok then
    fail(title, r.timed_out and "athena did not answer in time" or said(r))
  end
  local out, bad = rex.json.decode(r.stdout)
  if out == nil then
    fail(title, "athena's answer did not parse: " .. tostring(bad))
  end
  return out
end

-- An athena command that takes its time (a send, a retire). It runs detached, so the action's time limit never stops it half
-- way: past ACT_WAIT it carries on alone and the action says so.
local function athena_act(title, args, still)
  local h, err = rex.exec.start(athena_argv(title, args), { detach = true, env = ENV })
  if h == nil then
    fail(title, "athena did not run: " .. tostring(err))
  end
  local r = h:wait(ACT_WAIT)
  if r == nil then
    return nil, still
  end
  if not r.ok then
    fail(title, said(r))
  end
  return (r.stdout:gsub("%s+$", ""))
end

local function done(text)
  rex.log("info", "athena: " .. text)
  return { said = text }
end

-- The worker named, if it is live, else the one chosen from the live ones: never a question about a worker that isn't there.
local function live_worker(title, named, question, nothing)
  local live = athena_json(title, { "palette", "workers" })
  if named ~= nil then
    for _, w in ipairs(live.items) do
      if w.id == named then
        return named
      end
    end
    fail(title, "no live worker named " .. named .. "; " .. nothing .. ".")
  end
  if #live.items == 0 then
    fail(title, "no worker is live; " .. nothing .. ".")
  end
  local worker, why, detail = rex.ui.pick{ title = question, placeholder = "Filter workers", items = live.items }
  if worker == nil then
    unanswered(title, why, detail, nothing)
  end
  return worker
end

rex.action{
  name = "athena_answer",
  title = "athena: Answer a Decision…",
  description = "Answer one of the decisions waiting for you, as the dashboard would.",
  category = "athena",
  keywords = "athena decision answer dashboard needs you",
  args = { item = "string?", option = "string?", text = "string?" },
  run = function(ctx, args)
    local title = "Answer a Decision"
    local item, option, text = args.item, args.option, args.text
    if option ~= nil and text ~= nil then
      fail(title, "give an option or a text, not both; nothing was answered.")
    end
    if item == nil or (option == nil and text == nil) then
      local waiting = athena_json(title, { "palette", "decisions" })
      local why, detail
      if item == nil then
        if #waiting.items == 0 then
          fail(title, "no decision is waiting for you.")
        end
        item, why, detail = rex.ui.pick{ title = "Answer a decision", placeholder = "Filter decisions", items = waiting.items }
        if item == nil then
          unanswered(title, why, detail, "nothing was answered")
        end
      end
      local takes = waiting.answers[item]
      if takes == nil then
        fail(title, item .. " is not a decision waiting for your answer.")
      end
      local chosen
      if #takes.options == 0 then
        chosen, why, detail = rex.ui.input{ title = takes.title, placeholder = "Your reply", confirm = "Send reply" }
        chosen = chosen and { text = chosen }
      else
        chosen, why, detail = rex.ui.pick{
          title = takes.title,
          placeholder = takes.reply and "Choose, or type a reply" or "Choose an answer",
          items = takes.options,
          allow_other = takes.reply,
        }
      end
      if chosen == nil then
        unanswered(title, why, detail, "nothing was answered")
      end
      if type(chosen) == "table" then
        text = chosen.text
      else
        option = chosen
      end
    end
    local given = text ~= nil and ("--text=" .. text) or ("--option=" .. option)
    return done(athena_json(title, { "palette", "answer", item, given }).said)
  end,
}

rex.action{
  name = "athena_message",
  title = "athena: Message a Worker…",
  description = "Type a message to one of athena's live workers, the way athena sends one.",
  category = "athena",
  keywords = "athena worker message send tell",
  args = { worker = "string?", text = "string?" },
  run = function(ctx, args)
    local title = "Message a Worker"
    local worker = live_worker(title, args.worker, "Message a worker", "nothing was sent")
    local text = args.text
    if text == nil then
      local why, detail
      text, why, detail = rex.ui.input{ title = "Message " .. worker, placeholder = "What to tell it", confirm = "Send" }
      if text == nil then
        unanswered(title, why, detail, "nothing was sent")
      end
    end
    if text:match("^%s*$") then
      fail(title, "the message is empty; nothing was sent.")
    end
    local _, still = athena_act(title, { "send", "--", worker, text }, "still typing it to " .. worker .. ": athena finishes on its own")
    return done(still or ("Sent to " .. worker .. "."))
  end,
}

rex.action{
  name = "athena_open",
  title = "athena: Open…",
  description = "Show a worker's window, or open its pull request or an item's page.",
  category = "athena",
  keywords = "athena open show worker window pull request pr item dashboard",
  args = { target = "string?" },
  run = function(ctx, args)
    local title = "Open"
    local target = args.target
    if target == nil then
      local offered = athena_json(title, { "palette", "targets" })
      if #offered.items == 0 then
        fail(title, "athena has no worker, pull request or item to open.")
      end
      local why, detail
      target, why, detail = rex.ui.pick{ title = "Open", placeholder = "Filter workers, pull requests and items", items = offered.items }
      if target == nil then
        unanswered(title, why, detail, "nothing was opened")
      end
    end
    -- A window is shown by the app that ran the action, which Rex knows only when an app ran it: from `rex do`, a client action
    -- is delivered after the action has returned, if at all, so it would move the window and then fail. Refuse it first.
    if target:match("^worker:") and ctx.origin ~= "palette" and ctx.origin ~= "key" then
      fail(title, "showing a window needs the Rex app that asked: run it from the palette, or `rex -C <app> do athena_open "
        .. "target=" .. target .. " --origin palette`; nothing was opened.")
    end
    local opened = athena_json(title, { "palette", "open", target })
    if opened.focus ~= nil then
      -- Its window becomes its session's active one (a session this action isn't attached to until it asks), then the app
      -- that asked shows that session.
      local session = opened.focus.session_id
      local _, err = rex.call("session.attach", { session_id = session })
      if err == nil then
        _, err = rex.call("session.focus_window", { session_id = session, window_id = opened.focus.window_id })
      end
      if err ~= nil then
        fail(title, err)
      end
      rex.client.queue("session.select", { session_id = session })
    end
    return done(opened.said)
  end,
}

rex.action{
  name = "athena_retire",
  title = "athena: Retire a Worker…",
  description = "Retire one of athena's live workers: its work saved, its Claude stopped, its worktree and window removed.",
  category = "athena",
  keywords = "athena retire worker stop remove",
  args = { worker = "string?", confirm = "boolean?" },
  run = function(ctx, args)
    local title = "Retire a Worker"
    local worker = live_worker(title, args.worker, "Retire a worker", "nothing was retired")
    if args.confirm ~= true then
      local sure, why, detail = rex.ui.pick{
        title = "Retire " .. worker .. "?",
        items = {
          { id = "retire", title = "Retire " .. worker, icon = "gear",
            subtitle = "Back up its work, stop it, remove its worktree and window. Unsaved work stops the retire." },
          { id = "keep", title = "Keep " .. worker },
        },
      }
      if sure == nil then
        unanswered(title, why, detail, "nothing was retired")
      end
      if sure ~= "retire" then
        return done(worker .. " kept; nothing was retired.")
      end
    end
    local out, still = athena_act(title, { "retire", "--", worker }, "still retiring " .. worker .. ": athena finishes on its own")
    return done(still or out)
  end,
}

-- The line in Rex's config calls this with the state dir athena was installed for.
return function(opts)
  if type(opts) ~= "table" or type(opts.state_dir) ~= "string" or opts.state_dir == "" then
    error("athena palette: no state dir given", 0)
  end
  ENV = { ATHENA_STATE_DIR = opts.state_dir }
end
