# Create Initialization Feature For New Projects

When I create a new project, something that will be orchestrated by Orch, I typically go through a bootstrapping process.

I create a CLAUDE.md, I bootstrap a few planning documents, and then I start building out the project module by module.

I also initialize git, and eventually connect it to a remote service like GitHub.

I've been getting more and more into skills, infrastructure guidelines, technology choices, and deployment strategies in Claude as well, so decisions by Claude follow my preferences.

## The Ultimate Claude.md File

I envision creating a bootstrap utility that creates a powerful Claude.md file, with file pointers to design guides, infrastructure choices, and many other things.

These are static documents that don't change.

Then, there will be file pointers to documents that change often. It will be incredibly important to create a differentiation between 'static' and 'dynamic' documents, to encourage human changes, as well as changes from Claude Code itself.

## Resources

Here are links to raw README.md files. These repositories in general have a lot of valuable content, so it might be worth consuming the entire repository, rather than just the README.md

https://raw.githubusercontent.com/hesreallyhim/awesome-claude-code/refs/heads/main/README.md

https://raw.githubusercontent.com/VoltAgent/awesome-agent-skills/refs/heads/main/README.md

https://raw.githubusercontent.com/VoltAgent/awesome-voltagent/refs/heads/main/README.md

https://raw.githubusercontent.com/muratcankoylan/Agent-Skills-for-Context-Engineering/refs/heads/main/README.md

## Proposed Command

orch init

This will trigger a git init, creation of opinionated CLAUDE.md, .claude/rules files that are referenced as file pointers, and more.
