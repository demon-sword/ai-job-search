---
framework_version: 1.0.0
---

# Interview Preparation Guide

<!-- SETUP: STAR examples are personalized by running /setup based on your actual experience -->

## STAR Format

Structure answers as: **Situation** (context), **Task** (your responsibility), **Action** (what you did), **Result** (outcome).

Keep answers to 1-2 minutes. Be specific. End with what you learned or would do differently.

## Ready-Made STAR Examples

<!-- These are populated by /setup from your actual experience. Below are templates showing the format. -->

### 1. PulseAI Multi-Agent Platform (Multi-agent Architecture, LLM Orchestration)
**S:** At Dream11 Pulse, we needed to build a multi-tenant SaaS observability platform that could serve multiple products (FanCode, SouledStore, Fleek, OYO, Meesho) with domain-specific insights. The challenge was handling diverse data sources (ClickHouse, Athena, S3, MySQL) and providing natural-language querying across them.
**T:** Design and implement a multi-agent architecture that could route queries to the right data source, orchestrate multi-step reasoning, and produce executive-ready reports -- all while being production-ready with evaluation gates.
**A:** Architected a 5-persona agent system (Planner, Executor, Summary, Report, RCA) with a Features-as-Tools contract. Built dual-RAG (schema + docs) for cross-DB natural-language queries. Implemented Google ADK + MCP for agent orchestration, with dynamic tool routing across 47 MCP tools in 9 domains. Created 65-case golden dataset evaluation suite with Promptfoo/ADK gating deploys at 80% pass threshold. Added 3-layer production guardrails: PII redaction, anti-hallucination, leak defense.
**R:** Shipped PulseAI adopted by 5+ products; RCA agent surfaced $200K revenue-impacting bug at FanCode. Spec-to-ship workflow with Opus/Haiku advisor pattern adopted by 50-person engineering team. MCP server for 47 observability tools became pluggable platform.
**Use for:** "Describe a complex multi-agent system you built", "How do you handle LLM orchestration in production?", "Tell me about a time you built evaluation frameworks for AI systems", "How do you ensure reliability in agentic systems?"

### 2. Mobile Infrastructure Optimization (Performance Engineering, Scale)
**S:** Dream11's mobile app had 30s cold start latency and 125MB APK size, impacting 25-30M MAU. This was a critical user experience issue at massive scale.
**T:** Lead the optimization effort to reduce cold start latency and APK size while maintaining feature parity.
**A:** Profiled startup sequences, identified bottlenecks in initialization and dependency loading. Implemented lazy loading, code splitting, and native library optimization. Reduced APK from 125MB to 50MB (60% reduction) and cold start from 30s to 3s (90% reduction). Shipped GST compliance and Dreampay payment handlers processing INR 1Cr+ daily.
**R:** 90% cold start improvement (30s to 3s), 60% APK size reduction (125MB to 50MB), maintained 25-30M MAU with zero regressions. Payment handlers processing INR 1Cr+ daily.
**Use for:** "Describe a performance optimization project at scale", "How do you approach mobile infrastructure challenges?", "Tell me about a time you delivered measurable impact on user experience"

### 3. Developer Experience - Automated Testing (Developer Productivity, CI/CD)
**S:** Dream11's mobile team had no automated UI testing, leading to frequent regressions and slow release cycles for 25-30M MAU app.
**T:** Build comprehensive automated test infrastructure to catch regressions early and accelerate release pipeline.
**A:** Built Appium automated test suite with 100+ UI tests covering critical user flows. Implemented deep-link CI runners for targeted testing. Integrated into Jenkins pipeline with parallel execution. Reduced pipeline duration by 50%.
**R:** 100+ UI tests running in CI, 50% pipeline duration reduction, eliminated manual regression testing bottleneck, faster release cadence for 25-30M MAU app.
**Use for:** "How do you improve developer experience and productivity?", "Describe a CI/CD or testing infrastructure you built", "Tell me about a time you reduced release cycle time"

### 4. RCA Agent with Google ADK + MCP (Agentic AI, Production AI)
**S:** Production incidents at FanCode and other PulseAI tenants required rapid root cause analysis across multiple data sources, but manual investigation was slow and error-prone.
**T:** Build an autonomous RCA agent that could investigate incidents, correlate signals across data sources, and produce actionable root cause reports.
**A:** Built RCA agent using Google ADK + MCP with XenoRAG (Confluence to Qdrant). Agent autonomously queries logs, metrics, traces across ClickHouse/Athena/MySQL, correlates with recent deployments and config changes, generates executive summary with evidence.
**R:** RCA agent surfaced $200K revenue-impacting bug at FanCode that manual investigation missed. Reduced incident investigation time from hours to minutes. Became core feature of PulseAI platform.
**Use for:** "Tell me about an AI agent you built for production use", "How do you handle hallucination and reliability in LLM agents?", "Describe a time you used RAG in a production system"

### 5. Spec-to-Ship Workflow with Opus/Haiku Advisor (AI-Assisted Development, Team Adoption)
**S:** The 50-person engineering team at Dream11 wanted to adopt AI-assisted development but lacked structured workflows and quality gates.
**T:** Design and implement an AI-assisted development workflow with built-in quality controls that the whole team could adopt.
**A:** Built spec-to-ship workflow: requirements -> spec (Opus) -> implementation (Haiku) -> review (Opus) -> test -> deploy. Created Ralph loop for iterative refinement. Added Promptfoo evaluation gates. Documented patterns and trained team.
**R:** Workflow adopted by 50-person engineering team. Reduced spec-to-code time significantly. Quality gates caught issues before production. Became standard practice across team.
**Use for:** "How do you introduce AI tools to a team?", "Describe a developer productivity initiative you led", "Tell me about evaluating and adopting new AI tools"

### 6. Smart India Hackathon - Pragati ML Platform (ML Engineering, Competition)
**S:** Team built Pragati, an ML-based rainfall prediction platform for agriculture, competing against 100k+ participants in Smart India Hackathon.
**T:** Design end-to-end ML pipeline for rainfall prediction: data ingestion, model training, serving, and farmer-facing interface.
**A:** Built ML pipeline with weather data ingestion, feature engineering for spatiotemporal forecasting, model training and evaluation, and mobile/web interface for farmers. Used Python, scikit-learn, deployed on cloud.
**R:** Won National Award at Smart India Hackathon. Platform provided actionable rainfall predictions for agricultural planning.
**Use for:** "Tell me about an ML project you're proud of", "Describe an end-to-end ML system you built", "How do you handle ML in resource-constrained or real-world settings"

---
*[Add 1-2 more STAR examples from your experience as needed. Aim for 4-6 covering different competencies.]*

## Common Tough Questions

### "Why did you leave [previous company]?"
> Currently at Dream11 (Nov 2023-present). Not leaving -- seeking remote-first AI engineering role to work on GenAI/agentic systems while living near family in Delhi NCR. Current role is Mumbai-based.

### "You don't have [specific skill/experience]."
> Acknowledge the gap, bridge to adjacent experience, show willingness to learn. Example: "I haven't used [X] directly, but I've built similar systems with [Y] -- for instance, at Dream11 I [relevant example]. I learn new frameworks quickly; picked up Google ADK and MCP in weeks for PulseAI."

### "Where do you see yourself in 5 years?"
> Senior/Staff AI Engineer leading agentic AI initiatives, shaping technical direction for GenAI products. Deep technical IC track, not management. Want to be the person teams come to for hard AI production problems.

### "What's your biggest weakness?"
> Tendency to go deep on technical problems before validating business impact. Mitigation: now start every project with a 1-page problem framing doc (what, why, success metrics, risks) -- adopted from PulseAI spec-to-ship workflow.

### "Why this company specifically?"
> Customize per company. Must reference: specific projects, company values, market position, or team structure. Never give a generic answer.

## Questions You Should Ask Interviewers

### About the Role
- "What does a typical week look like in this role?"
- "What would success look like in the first 6 months?"
- "What's the biggest challenge the team is facing right now?"

### About the Team
- "How big is the team, and how do you divide work?"
- "What does the development/project lifecycle look like, from idea to production?"
- "How do you onboard new team members?"

### About Tech & Growth
- "What's your current tech stack for [relevant area]?"
- "Is there room to grow into more architectural or strategic decisions?"
- "How does the team stay current with new tools and methods?"

### About Culture (use these to prevent disappointment)
- "How would you describe the team culture?"
- "What does professional development look like here?"
- "Is there flexibility for remote/hybrid work?"
- "What's the balance between development/new projects and maintenance work?"
- "How would you describe the leadership style in this team?"
- "What do people who thrive here have in common?"

## Phone/Video Interview Tips
- Have STAR examples written out (use this file)
- Keep a glass of water nearby
- Smile when speaking (it changes your tone)
- Ask for clarification if a question is vague
- It's OK to take 5 seconds to think before answering
- End with: "Is there anything else you'd like to know about my background?"

## After the Application (Best Practice)

### Follow-Up Etiquette
- **Don't call to "stand out"** or to learn more about the role post-submission - this risks a negative impression
- If the employer specified a timeline, respect it and wait
- If no timeline was given and significant time has passed (2+ weeks), a brief call to ask about status is acceptable
- If you have genuinely new, relevant information to share, a short follow-up is fine

### Thank-You Notes
- When you receive any update (interview invitation, rejection, or status update), send a brief thank-you message
- Express appreciation for their time and the process
- Keep it short (2-3 sentences)

## Roleplay Guidelines
When the user asks for interview practice:
1. Ask which role/company to simulate
2. Start with easy warm-up questions ("Tell me about yourself")
3. Progress to role-specific technical questions
4. Include 1-2 behavioral questions using the competencies from the job posting
5. End with a tough question or curveball
6. After each answer, give brief feedback: what worked, what to sharpen
7. Suggest which STAR example would work best for each question