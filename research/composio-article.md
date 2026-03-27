The Self-Improving AI System That Built Itself
by prateek — Feb 23, 2026

I was trying to ship faster. I had a codebase, a backlog of things to build, and not enough hours in the day.
So I started running AI coding agents in parallel — give each one a task, let them write code, review the PRs, merge, repeat. I started with two or three. Then five. Then ten.
The agents were fast. The problem was me.
I couldn't keep up with them. I was the one checking if CI passed, reading review comments, copy-pasting errors back. I'd gone from writing code to babysitting the things that write code. That doesn't scale.
So I wrote some bash scripts to automate the coordination — about 2,500 lines that managed tmux sessions, git worktrees, and tab switching. Each agent got its own isolated tmux session and worktree. The orchestrator could spawn them, peek at what they were doing, forward CI failures back, and let me jump between sessions just by asking "take me to the tab for PR #1121."
It worked, barely.
Then I pointed the agents at the bash scripts themselves. They built v1 of a proper orchestrator. v1 managed the agents that built v2. And v2 has been improving itself since.

From Bash Scripts to Self-Improving System
The result: 40,000 lines of TypeScript, 17 plugins, 3,288 tests — built in 8 days, mostly by the agents the system orchestrates. Every commit has a git trailer identifying which AI model wrote it. There's no ambiguity about what humans did vs. what agents did.
We've open-sourced it: Agent Orchestrator.
The key thing to understand: the orchestrator itself is an AI agent. Not a dashboard. Not a cron job. Not a script that polls GitHub. It's an agent — it reads your codebase, understands your backlog, decides how to decompose a feature into parallelizable tasks, assigns each task to a coding agent, and monitors their progress.
When CI fails, it injects the failure back into the agent session — the agent reads the logs and fixes it. When a review comment comes in, it routes it to the right agent session with context. No human plumbing.
That's what makes this different from every "run agents in parallel" setup. The thing managing the agents is itself intelligent.

The Real Bottleneck in AI-Assisted Coding
Most people get the AI coding agent problem wrong. The agents can code. That's not the bottleneck. You are.
You spawn five tasks, go grab coffee, come back 20 minutes later and now you're just refreshing GitHub tabs — waiting for PRs, checking CI, reading review comments. Congratulations, you've automated engineering and replaced it with project management. Bad project management.
The orchestrator agent replaces you in that loop. Not with a script — with an actual AI agent that has context on every active session, every open PR, every CI run. It tracks everything, watches for failures, forwards review comments back to coding agents, and only pings you when something actually needs a human decision.
Once that bottleneck — your attention — goes away, things start compounding fast.
You open the dashboard to see status. But the orchestrator agent is already working — it's looked at all your workstreams and tells you: "This PR is blocking three other tasks, this CI failure is a flaky test, and this review comment is the one that actually matters." It's not showing you data. It's giving you decisions.
The other thing that matters: plug anything in. Different agent runtime? Different issue tracker? Different notification channel? Swap it. The orchestrator doesn't care if you use Claude Code or Aider, tmux or Docker, GitHub or Linear. Eight plugin slots, all replaceable.

The Timeline
People see "40K lines in 8 days" and assume I went into a cave. I have a day job. This was maybe ~3 days of actual focused work spread across 8 days, with agents filling the gaps.
The pattern was simple: set up sessions before bed, agents work overnight, review and merge in the morning before work, set up new sessions, repeat.
The standout day: Saturday Feb 14. 27 PRs merged in a single day. The entire platform shipped — core services, CLI, web dashboard, all 17 plugins, npm publishing. I was reviewing and merging PRs faster than I could read them, but every PR had passed CI and automated code review first.

Which Models Did What
Every commit tracks the model via git trailers:

Opus 4.6 handled the hard stuff — complex architecture, cross-package integrations.
Sonnet handled volume — plugin implementations, tests, docs.


Totals exceed 722 commits because some commits were written by one model and reviewed/fixed by another.


Fully Autonomous Code Review: 700 Comments, 1% Human
Agents don't just write code and throw it over the wall. There's a full automated review cycle:

Agent creates a PR and pushes code
Cursor Bugbot automatically reviews and posts inline comments
Agent reads comments, fixes the code, pushes again
Bugbot re-reviews

700 automated code review comments. Bugbot caught real stuff — shell injection via exec(), path traversal, unclosed intervals, missing null checks. The agents fixed ~68% immediately, explained away ~7% as intentional, and deferred ~4% to future PRs.

The ao-58 Story
The most dramatic example: PR #125, a dashboard redesign. It went through 12 CI failure → fix cycles. Each time, the agent got the failure output, diagnosed the issue (type errors, lint failures, test regressions), and pushed a fix. No human touched it. 12 rounds. Zero human intervention. Shipped clean.
All 41 CI failures across 9 branches were eventually self-corrected by agents. Overall CI success rate: 84.6%.

Architecture
The orchestrator uses a plugin system with 8 swappable slots:
Session Lifecycle

Tracker — pulls an issue (GitHub or Linear)
Workspace — creates an isolated worktree or clone
Runtime — starts a tmux session or process
Agent — (Claude Code, Aider, etc.) works autonomously
Terminal — lets you observe live via iTerm2 or web dashboard
SCM — creates PRs and enriches them with context
Reactions — auto re-spawns agents on CI failures or review comments
Notifier — pings you only when human judgment is needed

Don't use tmux? Use the process runtime. Don't use GitHub? Use Linear. Don't use Claude Code? Plug in Aider or Codex. Swap any piece.

Self-Healing CI: Agents That Fix Their Own Failures
The most useful feature. Automated responses to GitHub events:
yamlreactions:
  ci_failed:
    action: spawn_agent
    prompt: "CI failed on this PR. Read the failure logs and fix the issues."

  changes_requested:
    action: spawn_agent
    prompt: "Review comments have been posted. Address each comment and push fixes."

  approved:
    action: notify
    channel: slack
    message: "PR approved and ready to merge."
CI fails? Agent picks it up. Reviewer requests changes? Agent reads the comments and fixes the code. PR approved? You get a Slack notification. This is how those 41 CI failures got self-corrected — the reactions system just forwarded failures back to agents automatically.

The Inception: AI Agents Building Their Own Orchestrator
I had 30 concurrent agents working on Agent Orchestrator. They were building the TypeScript replacement while I was using the bash-script version to manage them. The thing being built was the thing managing its own construction.
What I actually did:

Architecture decisions (plugin slots, config schema, session lifecycle)
Spawning sessions and assigning issues
Reviewing PRs (mostly architecture, not line-by-line)
Resolving cross-agent conflicts (two agents editing the same file)
Judgment calls (reject this approach, try that one)

What agents did:

All implementation (40K lines of TypeScript)
All tests (3,288 test cases)
All PR creation (86 of 102 PRs)
All review comment fixes
All CI failure resolution

I never committed directly to a feature branch. Every line of code went through a PR.

Activity Detection
One of the trickier problems: figuring out what an agent is actually doing without asking it. Claude Code writes structured JSONL event files during every session. Instead of relying on agents to self-report (they lie, or at least get confused), the orchestrator reads these files directly:

Is the agent actively generating tokens?
Is it waiting for tool execution?
Is it idle?
Has it finished?

The agent-claude-code plugin knows how to parse Claude's session files. A future agent-aider plugin would read Aider's equivalent.

Web Dashboard
Next.js 15, Server-Sent Events for real-time updates. No polling.

Attention zones — sessions grouped by what needs your attention (failing CI, awaiting review, running fine)
Live terminal — xterm.js in the browser, showing the agent's actual terminal output in real time
Session detail — current file being edited, recent commits, PR status, CI status
Config discovery — automatically finds your ao.config.yaml and shows available sessions


The Self-Improving AI Loop
Every agent session generates signal. Which prompts led to clean PRs? Which ones spiraled into 12 CI failure cycles? Which patterns caused merge conflicts?
Most agent setups throw this signal away. Session finishes, you move on, next session starts from zero.
Agent Orchestrator has a self-improvement system (ao-52 — itself built by an agent) that logs performance, tracks session outcomes, and runs retrospectives. It learns which tasks succeed on the first try and which need tighter guardrails.

Agents build features → orchestrator observes what worked → adjusts how it manages future sessions → agents build better features.

The loop compounds. And since the agents built the orchestrator, and the orchestrator makes the agents more effective, and those agents keep improving the orchestrator — it's recursive. The tool is improving itself through the agents it manages.
I think this is why orchestration matters more than any individual agent improvement. The ceiling isn't "how good is Claude Code at TypeScript." It's "how good can a system get at deploying, observing, and improving dozens of agents working in parallel." That ceiling is much higher. And it rises every time the loop runs.

What's Next: Towards Fully Autonomous Software Engineering

Talk to your agents from anywhere. Right now you need to be at your desk. You should be able to message the orchestrator from Telegram or Slack — check status, approve a merge, redirect an agent — while you're on a walk.
Tighter mid-session feedback. Agents drift. They start solving the wrong problem, over-engineer a simple fix, go down rabbit holes. The orchestrator needs to check agent work against the original intent and inject course corrections before they've burned 20 minutes going the wrong direction.
Automatic escalation. Agent can't solve something? Escalate to orchestrator. Orchestrator needs judgment? Escalate to you. You only see things that genuinely need a human decision. Everything else resolves itself.
Beyond that: a reconciler for automatic conflict resolution between parallel agents, auto-rebase for long-running branches, Docker/K8s runtimes for cloud deployments, and a plugin marketplace for community contributions.


Try It
bashgit clone https://github.com/ComposioHQ/agent-orchestrator.git
cd agent-orchestrator
pnpm install && pnpm build
ao init --tracker github --agent claude-code --runtime tmux
ao start
Start the orchestrator, open the dashboard, and talk to it. Tell it what to build. It handles the rest — spawning agents, creating PRs, watching CI, forwarding review comments. You just make decisions.
We're looking for contributors: new plugins (agent runtimes, trackers, notifiers), Docker/K8s runtime, a reconciler for automatic conflict detection, and better escalation rules.

Repo: github.com/ComposioHQ/agent-orchestrator
Full metrics report: github.com/ComposioHQ/agent-orchestrator/releases/tag/metrics-v1
Interactive visualizations: pkarnal.com/ao-labs/


I'm building Agent Orchestrator and the developer tooling layer at Composio. If working on self-improving AI systems sounds like your kind of problem — we're hiring across SF and Bangalore: jobs.ashbyhq.com/composio
