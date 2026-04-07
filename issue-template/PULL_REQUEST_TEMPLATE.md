## 📋 Summary

<!-- Briefly describe what this PR does and why. Link to the issue it resolves. -->

Resolves: #<!-- issue number -->

---

## 🏷️ Type of Change

<!-- Check all that apply -->

- [ ] 🐛 Bug fix (non-breaking change that fixes an issue)
- [ ] ✨ New feature (non-breaking change that adds functionality)
- [ ] 🔌 New App AI Agent (new MCP integration)
- [ ] 💥 Breaking change (fix or feature that changes existing behavior)
- [ ] 📚 Documentation update
- [ ] 🧹 Refactor / code cleanup (no functional changes)
- [ ] ⚙️ CI / tooling / config change
- [ ] Other: <!-- describe -->

---

## 🧩 PlumoAI Component Affected

<!-- Which part of the platform does this PR touch? -->

- [ ] AI Employee (core behavior / reasoning)
- [ ] App AI Agent / MCP integration
- [ ] Project Management Workspace
- [ ] OpenClaw (workflow planning / execution)
- [ ] Authorization & Permissions
- [ ] Memory
- [ ] Presence (interaction interface)
- [ ] Docker / Deployment
- [ ] Documentation / Examples
- [ ] Other: <!-- describe -->

---

## 🔍 What Changed & Why

<!-- Explain the changes in enough detail for a reviewer to understand the intent and approach.
     For App AI Agents: describe what actions are exposed and which MCP server is used.
     For AI Employee changes: describe how behavior is affected. -->

### Before
<!-- What was the behavior / state before this change? -->

### After
<!-- What is the behavior / state after this change? -->

---

## 🧪 How Has This Been Tested?

<!-- Describe how you verified your changes work correctly. -->

- [ ] Tested locally with Docker (`docker pull plumoai/platform && docker run -p 3000:3000 plumoai/platform`)
- [ ] Tested in PlumoAI Cloud
- [ ] Tested with a real AI Employee assigned to a task
- [ ] Tested the App AI Agent connection with a live MCP server
- [ ] Tested edge cases (e.g. missing permissions, empty memory, failed tool calls)

**Test scenario(s):**
<!-- Briefly describe what you tested and the result -->

```
1. Created AI Employee with role: ...
2. Connected App AI Agent: ...
3. Assigned task: ...
4. Result: ...
```

---

## 📸 Screenshots / Demo (if applicable)

<!-- For UI changes, App AI Agent UIs, or new Presence types — attach screenshots or a short recording. -->

---

## ⚠️ Potential Risks or Side Effects

<!-- Are there any risks, regressions, or things reviewers should pay special attention to? -->

- [ ] None that I'm aware of
- [ ] May affect existing App AI Agent behavior — described below
- [ ] May affect AI Employee memory / context — described below
- [ ] May affect authorization / permission scopes — described below
- Other: <!-- describe -->

---

## 📚 Documentation

- [ ] I have updated or added documentation in `docs/` where needed
- [ ] I have added a usage example in `examples/` where applicable
- [ ] No documentation changes are needed for this PR

---

## ✅ Contributor Checklist

- [ ] My code follows the repository structure and contribution guidelines ([CONTRIBUTING.md](https://github.com/PlumoAI/plumoai/blob/main/CONTRIBUTING.md))
- [ ] I have removed all sensitive information (API keys, credentials, tokens) from this PR
- [ ] I have tested my changes and they work as expected
- [ ] My changes do not break existing functionality
- [ ] I have linked the related issue above
- [ ] I understand this contribution is subject to the [PlumoAI Community License](https://github.com/PlumoAI/plumoai/blob/main/LICENSE.md)

---

<!-- 
  🙏 Thank you for contributing to PlumoAI!
  Every contribution helps build the future of Autonomous AI Employees.
  
  Questions? Reach out at krishna@plumoai.com or open a Discussion.
-->
