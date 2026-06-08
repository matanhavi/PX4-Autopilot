---
name: gh-check-existing-work
description: Before starting work on a feature or bug, search GitHub issues and pull requests in the current repository and its upstream repo to find existing related work, duplicates, or active PRs.
---

# Check Existing GitHub Work

Use this skill when the user wants to start working on a new bug, feature, refactor, or investigation and wants to know whether similar work already exists.

## Goal

Before implementation, determine whether there is already:

- An open issue
- A closed issue with relevant context
- An open pull request
- A merged or closed pull request
- A discussion or prior attempt
- Similar work in the upstream repository

## Required behavior

Do not start coding before running the search.

First identify:

1. Current repository:
   ```bash
   gh repo view --json nameWithOwner,url,parent
2. If the repo is a fork, identify the upstream/parent repo from the parent field.

3. Ask the user for a short description of the intended work if it was not provided.

## Search strategy
Given the user's task description, extract 5-10 search phrases:

- exact feature name
- important function/module names
- error message, if any
- protocol/component name
- short synonyms
- relevant acronyms
- affected file or directory names, if known
- Search both the current repo and upstream repo.

For issues:
gh issue list -R OWNER/REPO --state all --search "QUERY" --json number,title,state,labels,author,createdAt,updatedAt,url

For pull requests:
gh pr list -R OWNER/REPO --state all --search "QUERY" --json number,title,state,author,createdAt,updatedAt,url,headRefName,baseRefName

Also use GitHub global issue/PR search when needed:
gh search issues "QUERY repo:OWNER/REPO" --json number,title,state,url,repository,updatedAt
gh search prs "QUERY repo:OWNER/REPO" --json number,title,state,url,repository,updatedAt

## Ranking
Rank results by relevance:

High relevance:
- Same component/module
- Same error message
- Same feature request
- Active open PR
- Recent activity
- Maintainer comments
- Linked issue/PR chain

Medium relevance:
- Same area but different implementation
- Old closed issue with useful context
- Similar PR that was abandoned

Low relevance:
- Same keywords but unrelated context
- Very old issue without clear connection

## Output format
Return a concise report:

### Existing work check
Summary
State one of:
- "Likely already being worked on"
- "Possibly related work exists"
- "No strong existing work found"
- "Search was inconclusive"

Best matches
For each relevant match:
- Type: Issue or PR
- Repo: owner/repo
- Number and title
- State
- Last updated
- Why it is relevant
- URL

Recommendation
Choose one:
- Continue implementation
- Comment on existing issue first
- Base work on existing PR
- Avoid duplicate work
- Open a new issue with links to related findings

