flower: a flight recorder for AI that does research

1. The problem
AI agents can now carry out much of a computational study on their own. Give one a scientific question and it will write the code, run simulations on a supercomputer, fix what breaks, and come back with an answer.

When work is long, nobody can check that answer.

This becomes more and more a bottleneck of my personal use. What the agent leaves behind is a pile of scripts, abandoned attempts and a conversation log thousands of lines long. No one can tell which calculation produced the final number, whether a failed run was quietly retried with new settings, or whether the same steps would give the same answer tomorrow, short of redoing the work by hand.

That falls on anyone who has to put their name to a result: a professor signing off a thesis, a journal reviewer judging a paper, a company R&D team handing simulation results to the engineers who will build on them. Today they spend days reconstructing what happened, or they trust it and hope.

It gets worse as agents get better, because results arrive faster than anyone can verify them. A thesis committee, a journal or a paying client cannot accept a number nobody can trace. Trust, but capability and speed, is becoming the bottleneck for AI in research.

2. Your workflow

flower works like a flight recorder that is also a recipe.

1. A researcher sets a goal, for example "reproduce the key results of this published paper", and hands it to an AI agent working inside flower.
2. The agent builds the study one step at a time. Each step is written down in plain language: what it is for, what it ran, what it found, and on which computer. Flower runs the work on a laptop, a lab workstation or a supercomputer. Long calculations keep going.
3. The agent can try things freely, when the study works, flower strips away the dead ends and keeps exactly the steps that produced the answer. Anyone can run that recipe on their own computers with one command, ensuring every work of the agent is replayable.

3. Trust, audit and governance
You see what the agent did, and why, on a visual map of the whole study. Click any step and it tells you first what that step was for, then what it did and what it found. A timeline replays every event in order. The software used is pinned down, so the same recipe always means the same tools.
When the agent gets something wrong, the mistake stays in the record. It cannot quietly overwrite a finished answer, and whoever approves a decision is named next to it.

Today the record takes people and agents in place of their own responsibilities, ensuring safe agentic productivity. Making identities verifiable is the first thing this grant would fund.

4. Team

I am the founder and builder: YinZhangHao Zhou (or Zhanghao Zhouyin somewhere), a physics PhD researcher at McGill University in Montréal. My research is large-scale computer simulation of materials. As co-first author I co-developed an AI model that predicts how electrons behave in materials, published in Nature Communications. I led a new AI architecture presented as a Spotlight at ICLR 2025, one of the top machine-learning conferences, and I wrote, on my own, the mathematical software that lets these simulations reach a million atoms. I also founded AI4S-Bench, an open project where scientists contribute real research problems to test whether AI can do science.

I built flower because I needed it. I now do much of my research alongside AI agents. They are fast and capable, and they leave nothing behind that my supervisor, my collaborators or I can audit a month later. I know from the inside what makes a computational result trustworthy, and what goes wrong when a machine does the work.

There are no formal advisors yet.
5. Where you are today

flower is a working product, tested on real published science, with no outside users yet.

- It is free and open source: https://github.com/floatingCatty/flower

- To test it, I gave an AI agent 15 published scientific studies to reproduce, each from the original paper to the final results, entirely inside flower. They span physics, chemistry and materials science, including a 2016 Science paper benchmarking 71 materials and a 2022 Science paper on twisted graphene. Most of each paper's claims were reproduced, and in most cases where a number did not match, the record showed why.

- More than 60 problems found in this testing were fixed, each becoming a permanent automated check.

The next step is other people. I want three research groups outside my own to use flower for their AI-driven work and tell me where it falls short.

6. Alternatives and competitors

Most people do nothing: a folder of scripts, a notebook, and trust.

Traditional workflow tools such as Snakemake (https://snakemake.github.io), Nextflow (https://www.nextflow.io) and AiiDA (https://www.aiida.net) are mature and widely used. They ask you to design the whole pipeline before you start. AI agents find the path by trying things, and that exploratory part is exactly what these tools never see.

Reproducibility platforms such as Code Ocean (https://codeocean.com) package a small, finished analysis. Weeks of supercomputer work are out of their reach.

AI monitoring tools such as Langfuse (https://langfuse.com) record what an AI said, which is a different thing from what the science did.

flower is built for the way AI does research: it records the work as it happens, keeps a person in charge, and delivers a result anyone can re-run and check.

7. Where this goes

AI agents are starting to produce a growing share of computational research. Each of those results will need a trustworthy record behind it, and flower aims to be the standard way that record is kept and handed over.

For science, the re-runnable recipe becomes part of the paper, so a reviewer or reader can check the result themselves. For industry, the same recipe is a deliverable a client can verify on their own computers, which is what R&D teams in materials, chemicals and semiconductors need before they act on a simulation.

The core stays free and open source. Revenue would come from a team edition and extension of infrastructure that connect cloud and cluster computation and close source domain research tools. 
8. Fit with Digital Science

flower sits where research is done and hands over to where it is published and shared. Its first users are researchers; next come the institutions, publishers and funders who need to trust computational results, and company R&D groups who need to audit them.

I want to work with Digital Science because a re-runnable recipe belongs next to the paper and data it supports, where it can be stored, cited and found. Figshare, and Digital Science's relationships with institutions and publishers, are where that happens. Your 2026 theme, AI that acts on research with trust that travels with it, describes the problem flower was built to solve.
9. Budget

Over twelve months: verified identities and tamper-evident records (£8,000); a shared team version so a whole group can follow the work (£7,000); one-click publishing of recipes to research repositories with a citable identifier (£4,000); support for three outside pilot groups (£4,000); computing time to keep re-testing on real science (£2,000). Together these turn a working tool into products that are mature and beneficial.


